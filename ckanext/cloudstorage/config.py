from __future__ import annotations

from ast import literal_eval

import ckan.plugins.toolkit as tk


OPTIONS = "ckanext.cloudstorage.driver_options"
DRIVER = "ckanext.cloudstorage.driver"
CONTAINER = "ckanext.cloudstorage.container_name"
SECURE_TTL = "ckanext.cloudstorage.secure_ttl"
USE_SECURE = "ckanext.cloudstorage.use_secure_urls"
LEAVE_FILES = "ckanext.cloudstorage.leave_files"
GUESS_MIMETYPE = "ckanext.cloudstorage.guess_mimetype"

def options() -> str:
    return literal_eval(tk.config[OPTIONS])

def driver() -> str:
    return tk.config[DRIVER]

def container() -> str:
    return tk.config[CONTAINER]

def secure_ttl() -> int:
    return tk.config[SECURE_TTL]

def use_secure() -> bool:
    return tk.config[USE_SECURE]

def leave_files() -> bool:
    return tk.config[LEAVE_FILES]

def guess_mimetype() -> bool:
    return tk.config[GUESS_MIMETYPE]
