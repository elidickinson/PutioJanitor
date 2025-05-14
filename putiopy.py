# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

import binascii
import io
import json
import logging
import os
import webbrowser
from datetime import datetime
from importlib.metadata import version
from urllib.parse import urlencode

import requests
import tus
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

try:
    __version__ = version("putio.py")
except:
    __version__ = "0.0.0"

KB = 1024
MB = 1024 * KB

# Read and write operations are limited to this chunk size.
# This can make a big difference when dealing with large files.
CHUNK_SIZE = 256 * KB

BASE_URL = None
UPLOAD_URL = None
TUS_UPLOAD_URL = None
ACCESS_TOKEN_URL = None
AUTHENTICATION_URL = None
AUTHORIZATION_URL = None

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

def _set_domain(domain="put.io", scheme="https"):
    global BASE_URL
    global UPLOAD_URL
    global TUS_UPLOAD_URL
    global ACCESS_TOKEN_URL
    global AUTHENTICATION_URL
    global AUTHORIZATION_URL

    api_base = "{scheme}://api.{domain}/v2".format(scheme=scheme, domain=domain)
    upload_base = "{scheme}://upload.{domain}".format(scheme=scheme, domain=domain)

    BASE_URL = api_base
    UPLOAD_URL = upload_base + "/v2/files/upload"
    TUS_UPLOAD_URL = upload_base + "/files/"
    ACCESS_TOKEN_URL = api_base + "/oauth2/access_token"
    AUTHENTICATION_URL = api_base + "/oauth2/authenticate"
    AUTHORIZATION_URL = (
        api_base + "/oauth2/authorizations/clients/{client_id}/{fingerprint}"
    )

_set_domain()

class APIError(Exception):
    """
    Must be created with following arguments:
        1. Response instance (requests.Response)
        2. Type of the error (str)
        3. Extra detail about the error (str, optional)
    """

    def __str__(self):
        s = "%s, %s, %d, %s" % (
            self.response.request.method,
            self.response.request.url,
            self.response.status_code,
            self.type,
        )
        if self.message:
            s += ", %r" % self.message
        return s

    @property
    def response(self):
        return self.args[0]

    @property
    def type(self):
        return self.args[1]

    @property
    def message(self):
        if len(self.args) > 2:
            return self.args[2]

class ClientError(APIError):
    pass

class ServerError(APIError):
    pass

class AuthHelper(object):

    def __init__(self, client_id, client_secret, redirect_uri, type="code"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.callback_url = redirect_uri
        self.type = type

    @property
    def authentication_url(self):
        """Redirect your users to here to authenticate them."""
        params = {
            "client_id": self.client_id,
            "response_type": self.type,
            "redirect_uri": self.callback_url,
        }
        return AUTHENTICATION_URL + "?" + urlencode(params)

    def open_authentication_url(self):
        webbrowser.open(self.authentication_url)

    def get_access_token(self, code):
        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": self.callback_url,
            "code": code,
        }
        response = requests.get(ACCESS_TOKEN_URL, params=params)
        return _process_response(response)["access_token"]

def create_access_token(client_id, client_secret, user, password, fingerprint=""):
    url = AUTHORIZATION_URL.format(client_id=client_id, fingerprint=fingerprint)
    data = {"client_secret": client_secret}
    auth = (user, password)
    response = requests.put(url, data=data, auth=auth)
    return _process_response(response)["access_token"]

def revoke_access_token(access_token):
    url = BASE_URL + "/oauth/grants/logout"
    headers = {"Authorization": "token %s" % access_token}
    response = requests.post(url, headers=headers)
    _process_response(response)

class Client(object):

    def __init__(self, access_token, use_retry=False, extra_headers=None, timeout=5):
        self.access_token = access_token
        self.session = requests.session()
        self.session.headers["User-Agent"] = "putio.py/%s" % __version__
        self.session.headers["Accept"] = "application/json"
        self.timeout = timeout
        if extra_headers:
            self.session.headers.update(extra_headers)

        if use_retry:
            # Retry maximum 10 times, backoff on each retry
            # Sleeps 1s, 2s, 4s, 8s, etc to a maximum of 120s between retries
            # Retries on HTTP status codes 500, 502, 503, 504
            retries = Retry(
                total=10, backoff_factor=1, status_forcelist=[500, 502, 503, 504]
            )

            # Use the retry strategy for all HTTPS requests
            self.session.mount("https://", HTTPAdapter(max_retries=retries))

        # Keep resource classes as attributes of client.
        # Pass client to resource classes so resource object
        # can use the client.
        attributes = {"client": self}
        self.File = type("File", (_File,), attributes)
        self.Subtitle = type("Subtitle", (_Subtitle,), attributes)
        self.Transfer = type("Transfer", (_Transfer,), attributes)
        self.Account = type("Account", (_Account,), attributes)

    def close(self):
        self.session.close()

    def request(
        self,
        path,
        method="GET",
        params=None,
        data=None,
        files=None,
        headers=None,
        raw=False,
        allow_redirects=True,
        stream=False,
        timeout=None,
    ):
        """
        Wrapper around requests.request()

        Prepends BASE_URL to path.
        Adds self.oauth_token to authorization header.
        Parses response as JSON and returns it.

        """
        if not headers:
            headers = {}

        if timeout is None:
            timeout = self.timeout

        # All requests must include oauth_token
        headers["Authorization"] = "token %s" % self.access_token

        if path.startswith(("http://", "https://")):
            url = path
        else:
            url = BASE_URL + path
        logger.debug("url: %s", url)

        response = self.session.request(
            method,
            url,
            params=params,
            data=data,
            files=files,
            headers=headers,
            allow_redirects=allow_redirects,
            stream=stream,
            timeout=self.timeout,
        )
        logger.debug("response: %s", response)
        if raw:
            return response

        return _process_response(response)

def _process_response(response):
    logger.debug("response: %s", response)
    logger.debug("content: %s", response.content)

    http_error_type = str(response.status_code)[0]
    exception_classes = {
        "2": None,
        "4": ClientError,
        "5": ServerError,
    }

    try:
        exception_class = exception_classes[http_error_type]
    except KeyError:
        raise ServerError(response, "UnknownStatusCode", str(response.status_code))

    if exception_class:
        try:
            d = _parse_content(response)
            error_type = d["error_type"]
            error_message = d["error_message"]
        except Exception:
            error_type = "UnknownError"
            error_message = None

        raise exception_class(response, error_type, error_message)

    return _parse_content(response)

def _parse_content(response):
    try:
        u = response.content.decode("utf-8")
    except ValueError:
        raise ServerError(response, "InvalidEncoding", "cannot decode as UTF-8")

    try:
        return json.loads(u)
    except ValueError:
        raise ServerError(response, "InvalidJSON")

def _str(s):
    """Convert Unicode to UTF-8 if possible."""
    try:
        return s.encode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return s

def strptime(date_str):
    """Convert ISO 8601 formatted string to datetime object."""
    date_str = date_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(date_str)
    except AttributeError:  # Python < 3.7
        date_str = date_str.replace("+00:00", "")
        return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")

class _BaseResource(object):

    client = None

    def __init__(self, resource_dict):
        """Constructs the object from a dict."""
        # All resources must have id and name attributes
        self.id = None
        self.name = None
        self.__dict__.update(resource_dict)
        try:
            self.created_at = strptime(self.created_at)
        except Exception:
            self.created_at = None

    def __str__(self):
        if self.name is None:
            return ""
        return str(self.name)

    def __repr__(self):
        # shorten name for display
        if self.name is None:
            name = ""
        else:
            name = self.name[:17] + "..." if len(self.name) > 20 else self.name
        return "<%s id=%r, name=%r>" % (self.__class__.__name__, self.id, name)

class _Account:
    client = None
    
    @classmethod
    def info(cls):
        """Get account information"""
        return cls.client.request("/account/info")["info"]

    @classmethod
    def settings(cls):
        """Get account settings"""
        return cls.client.request("/account/settings")["settings"]
        
    @classmethod
    def list_trash(cls):
        """List files in trash"""
        response = cls.client.request("/files/list", params={"trash": "true"})
        return response.get("files", [])
        
    @classmethod
    def delete_from_trash(cls, file_ids):
        """Permanently delete files from trash
        
        Args:
            file_ids: ID or comma-separated list of IDs to delete
        """
        return cls.client.request(
            "/files/delete", 
            method="POST", 
            data={"file_ids": file_ids},
            params={"trash": "true", "permanently": "true"}
        )

class _File(_BaseResource):

    @classmethod
    def get(cls, id):
        d = cls.client.request("/files/%i" % id, method="GET")
        t = d["file"]
        return cls(t)

    @classmethod
    def list(
        cls,
        parent_id=0,
        per_page=1000,
        sort_by=None,
        content_type=None,
        file_type=None,
        stream_url=False,
        stream_url_parent=False,
        mp4_stream_url=False,
        mp4_stream_url_parent=False,
        hidden=False,
        mp4_status=False,
    ):
        """List files and their properties.

        parent_id List files under a folder. If not specified, it will show files listed at the root folder

        """
        params = {
            "parent_id": parent_id,
            "per_page": str(per_page),
            "sort_by": sort_by or "",
            "content_type": content_type or "",
            "file_type": file_type or "",
            "stream_url": str(stream_url),
            "stream_url_parent": str(stream_url_parent),
            "mp4_stream_url": str(mp4_stream_url),
            "mp4_stream_url_parent": str(mp4_stream_url_parent),
            "hidden": str(hidden),
            "mp4_status": str(mp4_status),
        }
        d = cls.client.request("/files/list", params=params)
        files = d["files"]
        while d["cursor"]:
            d = cls.client.request(
                "/files/list/continue", method="POST", data={"cursor": d["cursor"]}
            )
            files.extend(d["files"])

        return [cls(f) for f in files]

    @classmethod
    def upload(cls, path, name=None, parent_id=0):
        """If the uploaded file is a torrent file, starts it as a transfer. This endpoint must be used with upload.put.io domain.
        name: override the file name
        parent_id: where to put the file
        """
        with io.open(path, "rb") as f:
            if name:
                files = {"file": (name, f)}
            else:
                files = {"file": f}
            d = cls.client.request(
                UPLOAD_URL, method="POST", data={"parent_id": parent_id}, files=files
            )

        try:
            return cls(d["file"])
        except KeyError:
            # server returns a transfer info if file is a torrent
            return cls.client.Transfer(d["transfer"])

    @classmethod
    def upload_tus(cls, path, name=None, parent_id=0):
        headers = {"Authorization": "token %s" % cls.client.access_token}
        metadata = {"parent_id": str(parent_id)}
        if name:
            metadata["name"] = name
        else:
            metadata["name"] = os.path.basename(path)
        with io.open(path, "rb") as f:
            tus.upload(
                f, TUS_UPLOAD_URL, file_name=name, headers=headers, metadata=metadata
            )

    @classmethod
    def search(cls, query, per_page=100):
        """
        Search makes a search request with the given query
        query: The keyword to search
        per_page: Number of files to be returned in response.
        """
        path = "/files/search"
        result = cls.client.request(path, params={"query": query, "per_page": per_page})
        files = result["files"]
        return [cls(f) for f in files]

    def dir(self):
        """List the files under directory."""
        return self.list(parent_id=self.id)

    def download(
        self, dest=".", delete_after_download=False, chunk_size=CHUNK_SIZE, save_as=""
    ):
        if self.content_type == "application/x-directory":
            self._download_directory(dest, delete_after_download, chunk_size, save_as)
        else:
            self._download_file(dest, delete_after_download, chunk_size, save_as)

    def _download_directory(self, dest, delete_after_download, chunk_size, save_as):
        name = _str(save_as) or _str(self.name)

        dest = os.path.join(dest, name)
        if not os.path.exists(dest):
            os.mkdir(dest)

        for sub_file in self.dir():
            sub_file.download(dest, delete_after_download, chunk_size)

        if delete_after_download:
            self.delete()

    def _verify_file(self, filepath):
        logger.info("verifying crc32...")
        filesize = os.path.getsize(filepath)
        if self.size != filesize:
            logging.warning("file {} has unexpected size: expected {}, found {}".format(filepath, self.size, filesize))
            return False

        if not hasattr(self, "crc32"):
            logger.warning("no crc32 checksum available, skipping")
            return True

        if self.crc32 is None:
            logger.warning("no crc32 checksum available, skipping")
            return True

        try:
            logger.debug("reading file for crc32 check...")
            checksum = 0
            with io.open(filepath, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    checksum = binascii.crc32(chunk, checksum)

            checksum = checksum & 0xffffffff
            logger.info(f"crc32 checksum: {checksum}")

            if self.crc32 != checksum:
                logger.warning(
                    "checksum mismatch. found: %s expected: %s" % (checksum, self.crc32)
                )
                return False
        except Exception as e:
            logger.warning("crc32 check failed: %s" % e)
            return False

        return True

    def _download_file(self, dest, delete_after_download, chunk_size, save_as):
        name = _str(save_as) or _str(self.name)
        filepath = os.path.join(dest, name)

        response = self.client.request(
            "/files/%s/download" % self.id, raw=True, stream=True
        )

        with io.open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:  # filter out keep-alive new chunks
                    f.write(chunk)

        response.close()

        logger.info("downloaded: %s" % filepath)

        if delete_after_download:
            if self._verify_file(filepath):
                self.delete()
            else:
                logger.warning("deleted is skipped because because of checksum mismatch")

    def delete(self):
        """Delete the file from put.io"""
        d = self.client.request("/files/delete", method="POST", data={"file_ids": str(self.id)})
        return d

    def move(self, parent_id):
        """Move the file to destination.
        parent_id: ID of the destination folder
        """
        d = self.client.request(
            "/files/move", method="POST", data={"file_ids": str(self.id), "parent_id": str(parent_id)}
        )
        return d

    def rename(self, name):
        """
        Rename the file
        name: New name for the file
        """
        d = self.client.request(
            "/files/rename", method="POST", data={"file_id": str(self.id), "name": name}
        )
        return d

    def convert_to_mp4(self):
        """Convert the file to MP4."""
        d = self.client.request("/files/%s/mp4" % self.id, method="POST")
        return d

    def get_mp4_status(self):
        """Get the MP4 conversion status of the file."""
        d = self.client.request("/files/%s/mp4" % self.id)
        return d

    def list_mp4s(self):
        """List available MP4s for the file."""
        d = self.client.request("/files/%s/mp4/list" % self.id)
        return d

    def get_download_url(self):
        """Get download URL of the file."""
        d = self.client.request("/files/%s/url" % self.id)
        return d["url"]

    def get_stream_url(self, tunnel=True, prefer_mp4=False):
        """Get stream URL of the file.

        Returns a URL that will be a playlist if the requested video
        needs to be converted, or the raw file otherwise.

        Use tunnel=True parameter to get a URL that should work for
        everyone (even those who need a tunnel). The default is True.

        Use prefer_mp4=True parameter to get the MP4 version if available.
        The default is False.
        """
        path = "/files/%s/stream" % self.id
        params = {"tunnel": tunnel, "mp4": prefer_mp4}
        response = self.client.request(path, params=params, raw=True)
        m3u_url = response.url

        logger.debug("m3u_url: %s", m3u_url)
        return m3u_url

    def get_subtitles(self):
        """Get the subtitles of the file."""
        d = self.client.request("/files/%s/subtitles" % self.id)
        return d

    def get_subtitle(self, key):
        """Get a subtitle from the file.

        key: Key of the subtitle file to fetch (You can get the keys with get_subtitles())
        """
        path = "/files/%s/subtitles/%s" % (self.id, key)
        d = self.client.request(path)
        return d

    def delete_subtitle(self, key):
        """Delete a subtitle from the file.

        key: Key of the subtitle file to delete (You can get the keys with get_subtitles())
        """
        path = "/files/%s/subtitles/%s/delete" % (self.id, key)
        d = self.client.request(path, method="POST")
        return d

    def upload_subtitle(self, path):
        """Upload a subtitle file.

        path: Path to the subtitle file
        """
        with io.open(path, "rb") as f:
            files = {"subtitle-file": f}
            d = self.client.request(
                "/files/%s/subtitles" % self.id, method="POST", files=files
            )
        return d

    def get_hls(self):
        """Extract HLS m3u8 URL from the file."""
        path = "/files/%s/hls/media.m3u8" % self.id
        r = self.client.request(path, raw=True)
        r.close()
        return r.url

    def share(self, friends):
        """Share with friends.

        friends: list of friend IDs or "all".
        """
        path = "/files/share"
        if isinstance(friends, str) and friends == "all":
            data = {"file_ids": str(self.id), "share_with": "all"}
        else:
            data = {"file_ids": str(self.id), "friends": ",".join(map(str, friends))}
        d = self.client.request(path, method="POST", data=data)
        return d

    def unshare(self, friends=None):
        """Unshare file with friends or all friends.

        friends: list of friend IDs or None.
        if friends is None, unshares with all friends.
        """
        path = "/files/{}/unshare".format(self.id)
        data = {}
        if isinstance(friends, list):
            data = {"friends": ",".join(map(str, friends))}
        d = self.client.request(path, method="POST", data=data)
        return d

    def shared_with(self):
        """List users this file is shared with."""
        path = "/files/{}/shared-with".format(self.id)
        d = self.client.request(path)
        return d

    def set_start_from(self, time):
        """Time in seconds from where the file should resume playing from"""
        path = "/files/{}/start-from".format(self.id)
        data = {"time": time}
        d = self.client.request(path, method="POST", data=data)
        return d

    def get_start_from(self):
        """Time in seconds from where the file should resume playing from"""
        path = "/files/{}/start-from".format(self.id)
        d = self.client.request(path)
        return d

    def delete_start_from(self):
        """Cancels resume point from"""
        path = "/files/{}/start-from/delete".format(self.id)
        d = self.client.request(path, method="POST")
        return d

    def done(self):
        """Marks file as finished watching"""
        path = "/files/{}/done".format(self.id)
        d = self.client.request(path, method="POST")
        return d

    def undone(self):
        """Marks file as not finished watching"""
        path = "/files/{}/undone".format(self.id)
        d = self.client.request(path, method="POST")
        return d

class _Subtitle(_BaseResource):

    @classmethod
    def list(cls, parent_id=0, kind="video"):
        """List subtitles. Parent id is required to list all files with subtitles"""
        path = "/files/list"
        params = {"parent_id": parent_id, "has_sublist": "subtitles", "kind": kind}
        d = cls.client.request(path, params=params)
        files = d["files"]
        return [cls(f) for f in files]

class _Transfer(_BaseResource):

    @classmethod
    def list(cls):
        d = cls.client.request("/transfers/list")
        transfers = d["transfers"]
        return [cls(t) for t in transfers]

    @classmethod
    def get(cls, id):
        d = cls.client.request("/transfers/%i" % id, method="GET")
        t = d["transfer"]
        return cls(t)

    @classmethod
    def add(cls, url, parent_id=0, extract=False, callback_url=None):
        """
        Add a transfer

        Args:
            url: URL to download (can be http(s), magnet)
            parent_id: folder id to upload to
            extract: whether to extract the download if possible
            callback_url: callback url to be notified when the transfer finishes
        """
        data = {"url": url, "save_parent_id": parent_id, "extract": extract}
        if callback_url:
            data["callback_url"] = callback_url
        d = cls.client.request("/transfers/add", method="POST", data=data)
        return cls(d["transfer"])

    def cancel(self):
        """Cancel the transfer."""
        return self.client.request(
            "/transfers/cancel", method="POST", data={"transfer_ids": self.id}
        )

    def clean(self):
        """Clean the transfer."""
        return self.client.request(
            "/transfers/clean", method="POST", data={"transfer_ids": self.id}
        )

    def remove(self):
        """Remove transfers. One per target_id please."""
        return self.client.request(
            "/transfers/remove", method="POST", data={"transfer_ids": self.id}
        )

    def retry(self):
        """Retry the transfer."""
        return self.client.request(
            "/transfers/retry", method="POST", data={"id": self.id}
        )