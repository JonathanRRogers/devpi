"""
Module for handling storage and proxy-streaming and caching of release files
for all indexes.

"""
from __future__ import unicode_literals
import hashlib
import mimetypes
from wsgiref.handlers import format_date_time
import os
import py
from devpi_common.types import cached_property, parse_hash_spec
from .log import threadlog

log = threadlog
_nodefault = object()

def get_default_hash_spec(content):
    #return "md5=" + hashlib.md5(content).hexdigest()
    return "sha256=" + hashlib.sha256(content).hexdigest()

def make_splitdir(hash_spec):
    parts = hash_spec.split("=")
    assert len(parts) == 2
    hash_value = parts[1]
    return hash_value[:3], hash_value[3:16]

def unicode_if_bytes(val):
    if isinstance(val, py.builtin.bytes):
        val = py.builtin._totext(val)
    return val


class FileStore:
    attachment_encoding = "utf-8"

    def __init__(self, xom):
        self.xom = xom
        self.keyfs = xom.keyfs
        self.rel_storedir = "+files"
        self.storedir = self.keyfs.basedir.join(self.rel_storedir)

    def maplink(self, link):
        if link.hash_spec:
            # we can only create 32K entries per directory
            # so let's take the first 3 bytes which gives
            # us a maximum of 16^3 = 4096 entries in the root dir
            a, b = make_splitdir(link.hash_spec)
            key = self.keyfs.STAGEFILE(user="root", index="pypi",
                                       hashdir_a=a, hashdir_b=b,
                                       filename=link.basename)
        else:
            parts = link.torelpath().split("/")
            assert parts
            dirname = "_".join(parts[:-1])
            key = self.keyfs.PYPIFILE_NOMD5(user="root", index="pypi",
                   dirname=dirname,
                   basename=parts[-1])
        entry = FileEntry(self.xom, key, readonly=False)
        entry.url = link.geturl_nofragment().url
        entry.eggfragment = link.eggfragment
        # verify checksum if the entry is fresh, a file exists
        # and the link specifies a checksum.  It's a situation
        # that shouldn't happen unless some manual file system
        # intervention or corruption happened
        if link.hash_spec and entry.file_exists() and not entry.hash_spec:
            threadlog.debug("verifying checksum of %s", entry.relpath)
            err = get_checksum_error(entry.file_get_content(), link.hash_spec)
            if err:
                threadlog.error(err)
                entry.file_delete()
        entry.hash_spec = unicode_if_bytes(link.hash_spec)
        return entry

    def get_file_entry(self, relpath, readonly=True):
        try:
            key = self.keyfs.tx.derive_key(relpath)
        except KeyError:
            return None
        return FileEntry(self.xom, key, readonly=readonly)

    def get_file_entry_raw(self, key, meta):
        return FileEntry(self.xom, key, meta=meta)

    def store(self, user, index, basename, file_content, dir_hash_spec=None):
        if dir_hash_spec is None:
            dir_hash_spec = get_default_hash_spec(file_content)
        hashdir_a, hashdir_b = make_splitdir(dir_hash_spec)
        key = self.keyfs.STAGEFILE(user=user, index=index,
                   hashdir_a=hashdir_a, hashdir_b=hashdir_b, filename=basename)
        entry = FileEntry(self.xom, key, readonly=False)
        entry.file_set_content(file_content)
        return entry


def metaprop(name):
    def fget(self):
        if self.meta is not None:
            return self.meta.get(name)
    def fset(self, val):
        val = unicode_if_bytes(val)
        if self.meta.get(name) != val:
            self.meta[name] = val
            self.key.set(self.meta)
    return property(fget, fset)


class FileEntry(object):
    class BadGateway(Exception):
        pass

    hash_spec = metaprop("hash_spec")  # e.g. "md5=120938012"
    eggfragment = metaprop("eggfragment")
    last_modified = metaprop("last_modified")
    url = metaprop("url")
    project = metaprop("project")
    version = metaprop("version")

    def __init__(self, xom, key, meta=_nodefault, readonly=True):
        self.xom = xom
        self.key = key
        self.relpath = key.relpath
        self.basename = self.relpath.split("/")[-1]
        self.readonly = readonly
        self._storepath = os.path.join(
            self.xom.filestore.rel_storedir,
            str(self.relpath))
        if meta is not _nodefault:
            self.meta = meta or {}

    @property
    def hash_value(self):
        return self.hash_spec.split("=", 1)[1]

    @property
    def hash_type(self):
        return self.hash_spec.split("=")[0]

    def check_checksum(self, content):
        if not self.hash_spec:
            return
        err = get_checksum_error(content, self.hash_spec)
        if err:
            return ValueError("%s: %s" %(self.relpath, err))

    def file_get_checksum(self, hash_type):
        return getattr(hashlib, hash_type)(self.file_get_content()).hexdigest()

    @property
    def tx(self):
        return self.key.keyfs.tx

    md5 = property(None, None)

    @cached_property
    def meta(self):
        return self.key.get(readonly=self.readonly)

    def file_exists(self):
        return self.tx.conn.io_file_exists(self._storepath)

    def file_delete(self):
        return self.tx.conn.io_file_delete(self._storepath)

    def file_size(self):
        return self.tx.conn.io_file_size(self._storepath)

    def __repr__(self):
        return "<FileEntry %r>" %(self.key)

    def file_open_read(self):
        return self.tx.conn.io_file_open(self._storepath)

    def file_get_content(self):
        return self.tx.conn.io_file_get(self._storepath)

    def file_os_path(self):
        return self.tx.conn.io_file_os_path(self._storepath)

    def file_set_content(self, content, last_modified=None, hash_spec=None):
        assert isinstance(content, bytes)
        if last_modified != -1:
            if last_modified is None:
                last_modified = unicode_if_bytes(format_date_time(None))
            self.last_modified = last_modified
        #else we are called from replica thread and just write outside
        if hash_spec:
            err = get_checksum_error(content, hash_spec)
            if err:
                raise ValueError(err)
        else:
            hash_spec = get_default_hash_spec(content)
        self.hash_spec = hash_spec
        self.tx.conn.io_file_set(self._storepath, content)
        # we make sure we always refresh the meta information
        # when we set the file content. Otherwise we might
        # end up only committing file content without any keys
        # changed which will not replay correctly at a replica.
        self.key.set(self.meta)

    def gethttpheaders(self):
        assert self.file_exists()
        headers = {}
        headers[str("last-modified")] = str(self.last_modified)
        m = mimetypes.guess_type(self.basename)[0]
        headers[str("content-type")] = str(m)
        headers[str("content-length")] = str(self.file_size())
        return headers

    def __eq__(self, other):
        try:
            return self.relpath == other.relpath and self.key == other.key
        except AttributeError:
            return False

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash(self.relpath)

    def delete(self, **kw):
        self.key.delete()
        self.meta = {}
        self.file_delete()

    def cache_remote_file(self):
        # we get and cache the file and some http headers from remote
        r = self.xom.httpget(self.url, allow_redirects=True)
        if r.status_code != 200:
            msg = "error %s getting %s" % (r.status_code, self.url)
            threadlog.error(msg)
            raise self.BadGateway(msg)
        log.info("reading remote: %s, target %s", r.url, self.relpath)
        content = r.raw.read()
        filesize = len(content)
        content_size = r.headers.get("content-length")
        err = None

        if content_size and int(content_size) != filesize:
            err = ValueError(
                      "%s: got %s bytes of %r from remote, expected %s" % (
                      self.relpath, filesize, r.url, content_size))
        if not err and not self.eggfragment:
            err = self.check_checksum(content)

        if err is not None:
            log.error(str(err))
            raise err

        self.file_set_content(content, r.headers.get("last-modified", None))

    def cache_remote_file_replica(self):
        # construct master URL with param
        assert self.url, "should have private files already: %s" % self.relpath
        threadlog.info("replica doesn't have file: %s", self.relpath)
        url = self.xom.config.master_url.joinpath(self.relpath).url

        # we do a head request to master and then wait for the file
        # to arrive through the replication machinery
        r = self.xom._httpsession.head(url)
        if r.status_code != 200:
            msg = "%s: received %s from master" %(url, r.status_code)
            threadlog.error(msg)
            raise self.BadGateway(msg)
        serial = int(r.headers["X-DEVPI-SERIAL"])
        keyfs = self.key.keyfs
        keyfs.notifier.wait_tx_serial(serial)
        keyfs.restart_read_transaction()  # use latest serial
        entry = self.xom.filestore.get_file_entry(self.relpath)
        if not entry.file_exists():
            msg = "%s: did not get file after waiting" % url
            threadlog.error(msg)
            raise self.BadGateway(msg)
        return entry


def get_checksum_error(content, hash_spec):
    hash_algo, hash_value = parse_hash_spec(hash_spec)
    hash_type = hash_spec.split("=")[0]
    digest = hash_algo(content).hexdigest()
    if digest != hash_value:
       return "%s mismatch, got %s, expected %s" % (hash_type, digest, hash_value)

