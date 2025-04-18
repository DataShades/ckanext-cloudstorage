#!/usr/bin/env python
# -*- coding: utf-8 -*-
import binascii
import cgi
import hashlib
import logging
import mimetypes
import os
import tempfile
import traceback
from ast import literal_eval
from datetime import datetime, timedelta
from urllib.parse import urljoin

import ckan.plugins as p
import libcloud.common.types as types
from ckan import model
from ckan.lib import munge
from libcloud.storage.providers import get_driver
from libcloud.storage.types import ObjectDoesNotExistError, Provider
from werkzeug.datastructures import FileStorage as FlaskFileStorage

import ckan.plugins.toolkit as tk
from . import config

log = logging.getLogger(__name__)

ALLOWED_UPLOAD_TYPES = (cgi.FieldStorage, FlaskFileStorage)
AWS_UPLOAD_PART_SIZE = 5 * 1024 * 1024


CONFIG_SECURE_TTL = "ckanext.cloudstorage.secure_ttl"
DEFAULT_SECURE_TTL = 3600


def config_secure_ttl():
    return config.secure_ttl()


def _get_underlying_file(wrapper):
    if isinstance(wrapper, FlaskFileStorage):
        return wrapper.stream
    return wrapper.file


def _md5sum(fobj):
    block_count = 0
    block = True
    md5string = b""
    while block:
        block = fobj.read(AWS_UPLOAD_PART_SIZE)
        if block:
            block_count += 1
            hash_obj = hashlib.md5()
            hash_obj.update(block)
            md5string = md5string + binascii.unhexlify(hash_obj.hexdigest())
        else:
            break
    fobj.seek(0, os.SEEK_SET)
    hash_obj = hashlib.md5()
    hash_obj.update(md5string)
    return hash_obj.hexdigest() + "-" + str(block_count)


class CloudStorage(object):
    def __init__(self):
        self.driver = get_driver(getattr(Provider, self.driver_name))(
            **self.driver_options
        )
        self._container = None

    def path_from_filename(self, rid, filename):
        raise NotImplementedError

    @property
    def container(self):
        """
        Return the currently configured libcloud container.
        """
        if self._container is None:
            self._container = self.driver.get_container(
                container_name=self.container_name
            )

        return self._container

    @property
    def driver_options(self):
        """
        A dictionary of options ckanext-cloudstorage has been configured to
        pass to the apache-libcloud driver.
        """
        return config.options()

    @property
    def driver_name(self):
        """
        The name of the driver (ex: AZURE_BLOBS, S3) that ckanext-cloudstorage
        is configured to use.


        .. note::

            This value is used to lookup the apache-libcloud driver to use
            based on the Provider enum.
        """
        return config.driver()

    @property
    def container_name(self):
        """
        The name of the container (also called buckets on some providers)
        ckanext-cloudstorage is configured to use.
        """
        return config.container()

    @property
    def use_secure_urls(self):
        """
        `True` if ckanext-cloudstroage is configured to generate secure
        one-time URLs to resources, `False` otherwise.
        """
        return config.use_secure()

    @property
    def leave_files(self):
        """
        `True` if ckanext-cloudstorage is configured to leave files on the
        provider instead of removing them when a resource/package is deleted,
        otherwise `False`.
        """
        return config.leave_files()

    @property
    def can_use_advanced_azure(self):
        """
        `True` if the `azure-storage` module is installed and
        ckanext-cloudstorage has been configured to use Azure, otherwise
        `False`.
        """
        # Are we even using Azure?
        if self.driver_name == "AZURE_BLOBS":
            try:
                # Yes? Is the azure-storage package available?
                from azure import storage

                # Shut the linter up.
                assert storage
                return True
            except ImportError:
                pass

        return False

    @property
    def can_use_advanced_aws(self):
        """
        `True` if the `boto` module is installed and ckanext-cloudstorage has
        been configured to use Amazon S3, otherwise `False`.
        """
        # Are we even using AWS?
        if "S3" in self.driver_name:
            if "host" not in self.driver_options:
                # newer libcloud versions(must-use for python3)
                # requires host for secure URLs
                return False
            try:
                # Yes? Is the boto package available?
                import boto3

                # Shut the linter up.
                assert boto3
                return True
            except ImportError:
                pass

        return False

    @property
    def guess_mimetype(self):
        """
        `True` if ckanext-cloudstorage is configured to guess mime types,
        `False` otherwise.
        """
        return config.guess_mimetype()


class ResourceCloudStorage(CloudStorage):
    def __init__(self, resource):
        """
        Support for uploading resources to any storage provider
        implemented by the apache-libcloud library.

        :param resource: The resource dict.
        """
        super(ResourceCloudStorage, self).__init__()

        self.filename = None
        self.old_filename = None
        self.file = None
        self.resource = resource

        upload_field_storage = resource.pop("upload", None)
        self._clear = resource.pop("clear_upload", None)
        multipart_name = resource.pop("multipart_name", None)

        # Check to see if a file has been provided
        if (
            isinstance(upload_field_storage, (ALLOWED_UPLOAD_TYPES))
            and upload_field_storage.filename
        ):
            self.filename = munge.munge_filename(upload_field_storage.filename)
            self.file_upload = _get_underlying_file(upload_field_storage)
            resource["url"] = self.filename
            resource["url_type"] = "upload"
            resource["last_modified"] = datetime.utcnow()
        elif multipart_name and self.can_use_advanced_aws:
            # This means that file was successfully uploaded and stored
            # at cloud.
            # Currently implemented just AWS version
            resource["url"] = munge.munge_filename(multipart_name)
            resource["url_type"] = "upload"
            resource["last_modified"] = datetime.utcnow()
        elif self._clear and resource.get("id"):
            # Apparently, this is a created-but-not-commited resource whose
            # file upload has been canceled. We're copying the behaviour of
            # ckaenxt-s3filestore here.
            old_resource = model.Session.query(model.Resource).get(resource["id"])

            self.old_filename = old_resource.url
            resource["url_type"] = ""

    def path_from_filename(self, rid, filename):
        """
        Returns a bucket path for the given resource_id and filename.

        :param rid: The resource ID.
        :param filename: The unmunged resource filename.
        """
        return os.path.join("resources", rid, munge.munge_filename(filename))

    def upload(self, id, max_size=10):
        """
        Complete the file upload, or clear an existing upload.

        :param id: The resource_id.
        :param max_size: Ignored.
        """
        if self.filename:
            if self.can_use_advanced_azure:
                from azure.storage import blob as azure_blob
                from azure.storage.blob.models import ContentSettings

                blob_service = azure_blob.BlockBlobService(
                    self.driver_options["key"], self.driver_options["secret"]
                )
                content_settings = None
                if self.guess_mimetype:
                    content_type, _ = mimetypes.guess_type(self.filename)
                    if content_type:
                        content_settings = ContentSettings(content_type=content_type)
                return blob_service.create_blob_from_stream(
                    container_name=self.container_name,
                    blob_name=self.path_from_filename(id, self.filename),
                    stream=self.file_upload,
                    content_settings=content_settings,
                )
            else:
                try:
                    file_upload = self.file_upload

                    # check if already uploaded
                    object_name = self.path_from_filename(id, self.filename)
                    try:
                        cloud_object = self.container.get_object(
                            object_name=object_name
                        )
                        log.debug(
                            "\t Object found, checking size %s: %s",
                            object_name,
                            cloud_object.size,
                        )
                        if os.path.isfile(self.filename):
                            file_size = os.path.getsize(self.filename)
                        else:
                            self.file_upload.seek(0, os.SEEK_END)
                            file_size = self.file_upload.tell()
                            self.file_upload.seek(0, os.SEEK_SET)

                        log.debug("\t - File size %s: %s", self.filename, file_size)
                        if file_size == int(cloud_object.size):
                            log.debug(
                                "\t Size fits, checking hash %s: %s",
                                object_name,
                                cloud_object.hash,
                            )
                            hash_file = hashlib.md5(self.file_upload.read()).hexdigest()
                            self.file_upload.seek(0, os.SEEK_SET)
                            log.debug(
                                "\t - File hash %s: %s",
                                self.filename,
                                hash_file,
                            )
                            # basic hash
                            if hash_file == cloud_object.hash:
                                log.debug(
                                    "\t => File found, matching hash, skipping upload"
                                )
                                return
                            # multipart hash
                            multi_hash_file = _md5sum(self.file_upload)
                            log.debug(
                                "\t - File multi hash %s: %s",
                                self.filename,
                                multi_hash_file,
                            )
                            if multi_hash_file == cloud_object.hash:
                                log.debug(
                                    "\t => File found, matching hash, skipping upload"
                                )
                                return
                        log.debug(
                            "\t Resource found in the cloud but outdated, uploading"
                        )
                    except ObjectDoesNotExistError:
                        log.debug("\t Resource not found in the cloud, uploading")

                    # If it's temporary file, we'd better convert it
                    # into FileIO. Otherwise libcloud will iterate
                    # over lines, not over chunks and it will really
                    # slow down the process for files that consist of
                    # millions of short linew
                    if isinstance(file_upload, tempfile.SpooledTemporaryFile):
                        file_upload.rollover()
                        try:
                            # extract underlying file
                            file_upload_iter = file_upload._file.detach()
                        except AttributeError:
                            # It's python2
                            file_upload_iter = file_upload._file
                    else:
                        file_upload_iter = iter(file_upload)
                    self.container.upload_object_via_stream(
                        iterator=file_upload_iter, object_name=object_name
                    )
                    log.debug("\t => UPLOADED %s: %s", self.filename, object_name)
                except (ValueError, types.InvalidCredsError) as err:
                    log.error(traceback.format_exc())
                    raise err

        elif self._clear and self.old_filename and not self.leave_files:
            # This is only set when a previously-uploaded file is replace
            # by a link. We want to delete the previously-uploaded file.
            try:
                self.container.delete_object(
                    self.container.get_object(
                        self.path_from_filename(id, self.old_filename)
                    )
                )
            except ObjectDoesNotExistError:
                # It's possible for the object to have already been deleted, or
                # for it to not yet exist in a committed state due to an
                # outstanding lease.
                return

    def get_url_from_filename(self, rid, filename, content_type=None):
        path = self.path_from_filename(rid, filename)

        return self.get_url_by_path(path, content_type)

    def get_url_by_path(self, path, content_type=None):
        """
        Retrieve a publically accessible URL for the given path

        .. note::

            Works for Azure and any libcloud driver that implements
            support for get_object_cdn_url (ex: AWS S3).

        :param path: The resource name on cloud.
        :param content_type: Optionally a Content-Type header.

        :returns: Externally accessible URL or None.
        """
        # If advanced azure features are enabled, generate a temporary
        # shared access link instead of simply redirecting to the file.
        if self.can_use_advanced_azure and self.use_secure_urls:
            from azure.storage import blob as azure_blob

            blob_service = azure_blob.BlockBlobService(
                self.driver_options["key"], self.driver_options["secret"]
            )

            return blob_service.make_blob_url(
                container_name=self.container_name,
                blob_name=path,
                sas_token=blob_service.generate_blob_shared_access_signature(
                    container_name=self.container_name,
                    blob_name=path,
                    expiry=datetime.utcnow() + timedelta(seconds=config_secure_ttl()),
                    permission=azure_blob.BlobPermissions.READ,
                ),
            )
        elif self.can_use_advanced_aws and self.use_secure_urls:
            from boto3 import client
            from boto3.session import Config

            params = {
                "aws_access_key_id": self.driver_options["key"],
                "aws_secret_access_key": self.driver_options["secret"],
                "config": Config(signature_version="s3v4"),
            }
            if "region" in self.driver_options:
                region = self.driver_options["region"]
                params.update(
                    {
                        "region_name": region,
                        "endpoint_url": f"https://s3.{region}.amazonaws.com",
                    }
                )

            # endpoint_url = self.driver_options["host"] ?
            s3 = client("s3", **params)
            resp = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.container_name, "Key": path},
                ExpiresIn=config_secure_ttl(),
            )
            return resp

        # Find the object for the given key.
        try:
            obj = self.container.get_object(path)
        except ObjectDoesNotExistError:
            return
        if obj is None:
            return

        # Not supported by all providers!
        try:
            return self.driver.get_object_cdn_url(obj)
        except NotImplementedError:
            if "S3" in self.driver_name:
                return urljoin(
                    "https://" + self.driver.connection.host,
                    "{container}/{path}".format(
                        container=self.container_name,
                        path=path,
                    ),
                )
            # This extra 'url' property isn't documented anywhere, sadly.
            # See azure_blobs.py:_xml_to_object for more.
            elif "url" in obj.extra:
                return obj.extra["url"]
            raise

    @property
    def package(self):
        return model.Package.get(self.resource["package_id"])
