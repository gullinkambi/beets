# This file is part of beets.
# Copyright 2013, Adrian Sampson.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

"""The core data store and collection logic for beets.
"""
import sqlite3
import os
import re
import sys
import logging
import shlex
import unicodedata
import threading
import contextlib
import traceback
import time
from collections import defaultdict
from unidecode import unidecode
from beets.mediafile import MediaFile
from beets import plugins
from beets import util
from beets.util import bytestring_path, syspath, normpath, samefile
from beets.util.functemplate import Template
import beets
from datetime import datetime


# Fields in the "items" database table; all the metadata available for
# items in the library. These are used directly in SQL; they are
# vulnerable to injection if accessible to the user.
# Each tuple has the following values:
# - The name of the field.
# - The (Python) type of the field.
# - Is the field writable?
# - Does the field reflect an attribute of a MediaFile?
ITEM_FIELDS = [
    ('id',          int,   False, False),
    ('path',        bytes, False, False),
    ('album_id',    int,   False, False),

    ('title',                unicode, True, True),
    ('artist',               unicode, True, True),
    ('artist_sort',          unicode, True, True),
    ('artist_credit',        unicode, True, True),
    ('album',                unicode, True, True),
    ('albumartist',          unicode, True, True),
    ('albumartist_sort',     unicode, True, True),
    ('albumartist_credit',   unicode, True, True),
    ('genre',                unicode, True, True),
    ('composer',             unicode, True, True),
    ('grouping',             unicode, True, True),
    ('year',                 int,     True, True),
    ('month',                int,     True, True),
    ('day',                  int,     True, True),
    ('track',                int,     True, True),
    ('tracktotal',           int,     True, True),
    ('disc',                 int,     True, True),
    ('disctotal',            int,     True, True),
    ('lyrics',               unicode, True, True),
    ('comments',             unicode, True, True),
    ('bpm',                  int,     True, True),
    ('comp',                 bool,    True, True),
    ('mb_trackid',           unicode, True, True),
    ('mb_albumid',           unicode, True, True),
    ('mb_artistid',          unicode, True, True),
    ('mb_albumartistid',     unicode, True, True),
    ('albumtype',            unicode, True, True),
    ('label',                unicode, True, True),
    ('acoustid_fingerprint', unicode, True, True),
    ('acoustid_id',          unicode, True, True),
    ('mb_releasegroupid',    unicode, True, True),
    ('asin',                 unicode, True, True),
    ('catalognum',           unicode, True, True),
    ('script',               unicode, True, True),
    ('language',             unicode, True, True),
    ('country',              unicode, True, True),
    ('albumstatus',          unicode, True, True),
    ('media',                unicode, True, True),
    ('albumdisambig',        unicode, True, True),
    ('disctitle',            unicode, True, True),
    ('encoder',              unicode, True, True),
    ('rg_track_gain',        float,   True, True),
    ('rg_track_peak',        float,   True, True),
    ('rg_album_gain',        float,   True, True),
    ('rg_album_peak',        float,   True, True),
    ('original_year',        int,     True, True),
    ('original_month',       int,     True, True),
    ('original_day',         int,     True, True),

    ('length',      float,    False, True),
    ('bitrate',     int,      False, True),
    ('format',      unicode,  False, True),
    ('samplerate',  int,      False, True),
    ('bitdepth',    int,      False, True),
    ('channels',    int,      False, True),
    ('mtime',       int,      False, False),
    ('added',       datetime, False, False),
]
ITEM_KEYS_WRITABLE = [f[0] for f in ITEM_FIELDS if f[3] and f[2]]
ITEM_KEYS_META     = [f[0] for f in ITEM_FIELDS if f[3]]
ITEM_KEYS          = [f[0] for f in ITEM_FIELDS]


# Database fields for the "albums" table.
# The third entry in each tuple indicates whether the field reflects an
# identically-named field in the items table.
ALBUM_FIELDS = [
    ('id',      int,      False),
    ('artpath', bytes,    False),
    ('added',   datetime, True),

    ('albumartist',        unicode, True),
    ('albumartist_sort',   unicode, True),
    ('albumartist_credit', unicode, True),
    ('album',              unicode, True),
    ('genre',              unicode, True),
    ('year',               int,     True),
    ('month',              int,     True),
    ('day',                int,     True),
    ('tracktotal',         int,     True),
    ('disctotal',          int,     True),
    ('comp',               bool,    True),
    ('mb_albumid',         unicode, True),
    ('mb_albumartistid',   unicode, True),
    ('albumtype',          unicode, True),
    ('label',              unicode, True),
    ('mb_releasegroupid',  unicode, True),
    ('asin',               unicode, True),
    ('catalognum',         unicode, True),
    ('script',             unicode, True),
    ('language',           unicode, True),
    ('country',            unicode, True),
    ('albumstatus',        unicode, True),
    ('media',              unicode, True),
    ('albumdisambig',      unicode, True),
    ('rg_album_gain',      float,   True),
    ('rg_album_peak',      float,   True),
    ('original_year',      int,     True),
    ('original_month',     int,     True),
    ('original_day',       int,     True),
]
ALBUM_KEYS = [f[0] for f in ALBUM_FIELDS]
ALBUM_KEYS_ITEM = [f[0] for f in ALBUM_FIELDS if f[2]]


# SQLite type names.
SQLITE_TYPES = {
    int:      'INT',
    float:    'REAL',
    datetime: 'FLOAT',
    bytes:    'BLOB',
    unicode:  'TEXT',
    bool:     'INT',
}
SQLITE_KEY_TYPE = 'INTEGER PRIMARY KEY'


# Default search fields for each model.
ALBUM_DEFAULT_FIELDS = ('album', 'albumartist', 'genre')
ITEM_DEFAULT_FIELDS = ALBUM_DEFAULT_FIELDS + ('artist', 'title', 'comments')


# Special path format key.
PF_KEY_DEFAULT = 'default'


# Logger.
log = logging.getLogger('beets')
if not log.handlers:
    log.addHandler(logging.StreamHandler())
log.propagate = False  # Don't propagate to root handler.


# A little SQL utility.
def _orelse(exp1, exp2):
    """Generates an SQLite expression that evaluates to exp1 if exp1 is
    non-null and non-empty or exp2 otherwise.
    """
    return ('(CASE {0} WHEN NULL THEN {1} '
                      'WHEN "" THEN {1} '
                      'ELSE {0} END)').format(exp1, exp2)


# Path element formatting for templating.
def format_for_path(value, key=None):
    """Sanitize the value for inclusion in a path: replace separators
    with _, etc. Doesn't guarantee that the whole path will be valid;
    you should still call `util.sanitize_path` on the complete path.
    """
    if isinstance(value, basestring):
        if isinstance(value, str):
            value = value.decode('utf8', 'ignore')
    elif key in ('track', 'tracktotal', 'disc', 'disctotal'):
        # Pad indices with zeros.
        value = u'%02i' % (value or 0)
    elif key == 'year':
        value = u'%04i' % (value or 0)
    elif key in ('month', 'day'):
        value = u'%02i' % (value or 0)
    elif key == 'bitrate':
        # Bitrate gets formatted as kbps.
        value = u'%ikbps' % ((value or 0) // 1000)
    elif key == 'samplerate':
        # Sample rate formatted as kHz.
        value = u'%ikHz' % ((value or 0) // 1000)
    elif key in ('added', 'mtime'):
        # Times are formatted to be human-readable.
        value = time.strftime(beets.config['time_format'].get(unicode),
                              time.localtime(value))
        value = unicode(value)
    elif value is None:
        value = u''
    else:
        value = unicode(value)

    return value



# Common base class for database elements.


class Model(object):
    """An abstract object representing an object in the database. Model
    objects act like dictionaries (i.e., the allow subscript access like
    ``obj['field']``). The same field set is available via attribute
    access as a shortcut (i.e., ``obj.field``). Three kinds of attributes are
    available:

    * **Fixed attributes** come from a predetermined list of field
      names. These fields correspond to SQLite table columns and are
      thus fast to read, write, and query.
    * **Flexible attributes** are free-form and do not need to be listed
      ahead of time.
    * **Computed attributes** are read-only fields computed by a getter
      function provided by a plugin.

    Access to all three field types is uniform: ``obj.field`` works the
    same regardless of whether ``field`` is fixed, flexible, or
    computed.

    Model objects can optionally be associated with a `Library` object,
    in which case they can be loaded and stored from the database. Dirty
    flags are used to track which fields need to be stored.
    """

    # Abstract components (to be provided by subclasses).

    _table = None
    """The main SQLite table name.
    """

    _flex_table = None
    """The flex field SQLite table name.
    """

    _fields = ()
    """The available "fixed" fields on this type.
    """

    _bytes_keys = ('path', 'artpath')
    """Keys whose values should be stored as raw bytes blobs rather than
    strings.
    """

    _search_fields = ()
    """The fields that should be queried by default by unqualified query
    terms.
    """

    @classmethod
    def _getters(cls):
        """Return a mapping from field names to getter functions.
        """
        # We could cache this if it becomes a performance problem to
        # gather the getter mapping every time.
        raise NotImplementedError()


    # Basic operation.

    def __init__(self, lib=None, **values):
        """Create a new object with an optional Library association and
        initial field values.
        """
        self._lib = lib
        self._dirty = set()
        self._values_fixed = {}
        self._values_flex = {}

        # Initial contents.
        self.update(values)
        self.clear_dirty()

    def __repr__(self):
        return '{0}({1})'.format(
            type(self).__name__,
            ', '.join('{0}={1!r}'.format(k, v) for k, v in dict(self).items()),
        )

    def clear_dirty(self):
        """Mark all fields as *clean* (i.e., not needing to be stored to
        the database).
        """
        self._dirty = set()

    def _check_db(self, need_id=True):
        """Ensure that this object is associated with a database row: it
        has a reference to a library (`_lib`) and an id. A ValueError
        exception is raised otherwise.
        """
        if not self._lib:
            raise ValueError('{0} has no library'.format(type(self).__name__))
        if need_id and not self.id:
            raise ValueError('{0} has no id'.format(type(self).__name__))


    # Essential field accessors.

    def __getitem__(self, key):
        """Get the value for a field. Raise a KeyError if the field is
        not available.
        """
        getters = self._getters()
        if key in getters:  # Computed.
            return getters[key](self)
        elif key in self._fields:  # Fixed.
            return self._values_fixed.get(key)
        elif key in self._values_flex:  # Flexible.
            return self._values_flex[key]
        else:
            raise KeyError(key)

    def __setitem__(self, key, value):
        """Assign the value for a field.
        """
        source = self._values_fixed if key in self._fields \
                 else self._values_flex
        old_value = source.get(key)
        source[key] = value
        if old_value != value:
            self._dirty.add(key)

    def keys(self, computed=False):
        """Get a list of available field names for this object. The
        `computed` parameter controls whether computed (plugin-provided)
        fields are included in the key list.
        """
        base_keys = list(self._fields) + self._values_flex.keys()
        if computed:
            return base_keys + self._getters().keys()
        else:
            return base_keys


    # Act like a dictionary.

    def update(self, values):
        """Assign all values in the given dict.
        """
        for key, value in values.items():
            self[key] = value

    def items(self):
        """Iterate over (key, value) pairs that this object contains.
        Computed fields are not included.
        """
        for key in self:
            yield key, self[key]

    def get(self, key, default=None):
        """Get the value for a given key or `default` if it does not
        exist.
        """
        if key in self:
            return self[key]
        else:
            return default

    def __contains__(self, key):
        """Determine whether `key` is an attribute on this object.
        """
        return key in self.keys(True)

    def __iter__(self):
        """Iterate over the available field names (excluding computed
        fields).
        """
        return iter(self.keys())


    # Convenient attribute access.

    def __getattr__(self, key):
        if key.startswith('_'):
            raise AttributeError('model has no attribute {0!r}'.format(key))
        else:
            try:
                return self[key]
            except KeyError:
                raise AttributeError('no such field {0!r}'.format(key))

    def __setattr__(self, key, value):
        if key.startswith('_'):
            super(Model, self).__setattr__(key, value)
        else:
            self[key] = value


    # Database interaction (CRUD methods).

    def store(self):
        """Save the object's metadata into the library database.
        """
        self._check_db()

        # Build assignments for query.
        assignments = ''
        subvars = []
        for key in self._fields:
            if key != 'id' and key in self._dirty:
                assignments += key + '=?,'
                value = self[key]
                # Wrap path strings in buffers so they get stored
                # "in the raw".
                if key in self._bytes_keys and isinstance(value, str):
                    value = buffer(value)
                subvars.append(value)
        assignments = assignments[:-1]  # Knock off last ,

        with self._lib.transaction() as tx:
            # Main table update.
            if assignments:
                query = 'UPDATE {0} SET {1} WHERE id=?'.format(
                    self._table, assignments
                )
                subvars.append(self.id)
                tx.mutate(query, subvars)

            # Flexible attributes.
            for key, value in self._values_flex.items():
                if key in self._dirty:
                    tx.mutate(
                        'INSERT INTO {0} '
                        '(entity_id, key, value) '
                        'VALUES (?, ?, ?);'.format(self._flex_table),
                        (self.id, key, value),
                    )

        self.clear_dirty()

    def load(self):
        """Refresh the object's metadata from the library database.
        """
        self._check_db()
        stored_obj = self._lib._get(type(self), self.id)
        self.update(dict(stored_obj))
        self.clear_dirty()

    def remove(self):
        """Remove the object's associated rows from the database.
        """
        self._check_db()
        with self._lib.transaction() as tx:
            tx.mutate(
                'DELETE FROM {0} WHERE id=?'.format(self._table),
                (self.id,)
            )
            tx.mutate(
                'DELETE FROM {0} WHERE entity_id=?'.format(self._flex_table),
                (self.id,)
            )

    def add(self, lib=None):
        """Add the object to the library database. This object must be
        associated with a library; you can provide one via the `lib`
        parameter or use the currently associated library.

        The object's `id` and `added` fields are set along with any
        current field values.
        """
        if lib:
            self._lib = lib
        self._check_db(False)

        with self._lib.transaction() as tx:
            new_id = tx.mutate(
                'INSERT INTO {0} DEFAULT VALUES'.format(self._table)
            )
            self.id = new_id
            self.added = time.time()

            # Mark every non-null field as dirty and store.
            for key in self:
                if self[key] is not None:
                    self._dirty.add(key)
            self.store()


    # Formatting and templating.

    def _get_formatted(self, key, for_path=False):
        """Get a field value formatted as a string (`unicode` object)
        for display to the user. If `for_path` is true, then the value
        will be sanitized for inclusion in a pathname (i.e., path
        separators will be removed from the value).
        """
        value = self.get(key)

        # FIXME this will get replaced with more sophisticated
        # (type-based) formatting logic.
        value = format_for_path(value, key)

        if for_path:
            sep_repl = beets.config['path_sep_replace'].get(unicode)
            for sep in (os.path.sep, os.path.altsep):
                if sep:
                    value = value.replace(sep, sep_repl)

        return value

    def _formatted_mapping(self, for_path=False):
        """Get a mapping containing all values on this object formatted
        as human-readable strings.
        """
        # In the future, this could be made "lazy" to avoid computing
        # fields unnecessarily.
        out = {}
        for key in self.keys(True):
            out[key] = self._get_formatted(key, for_path)
        return out

    def evaluate_template(self, template, for_path=False):
        """Evaluate a template (a string or a `Template` object) using
        the object's fields. If `for_path` is true, then no new path
        separators will be added to the template.
        """
        # Build value mapping.
        mapping = self._formatted_mapping(for_path)

        # Get template functions.
        funcs = DefaultTemplateFunctions(self, self._lib).functions()
        funcs.update(plugins.template_funcs())

        # Perform substitution.
        if isinstance(template, basestring):
            template = Template(template)
        return template.substitute(mapping, funcs)


# Item and Album concrete classes.


class Item(Model):
    _fields = ITEM_KEYS
    _table = 'items'
    _flex_table = 'item_attributes'
    _search_fields = ITEM_DEFAULT_FIELDS

    @classmethod
    def _getters(cls):
        return plugins.item_field_getters()

    @classmethod
    def from_path(cls, path):
        """Creates a new item from the media file at the specified path.
        """
        # Initiate with values that aren't read from files.
        i = cls(album_id=None)
        i.read(path)
        i.mtime = i.current_mtime()  # Initial mtime.
        return i

    def __setitem__(self, key, value):
        """Set the item's value for a standard field or a flexattr.
        """
        # Encode unicode paths and read buffers.
        if key == 'path':
            if isinstance(value, unicode):
                value = bytestring_path(value)
            elif isinstance(value, buffer):
                value = str(value)

        if key in ITEM_KEYS_WRITABLE:
            self.mtime = 0  # Reset mtime on dirty.

        super(Item, self).__setitem__(key, value)

    def update(self, values):
        """Sett all key/value pairs in the mapping. If mtime is
        specified, it is not reset (as it might otherwise be).
        """
        super(Item, self).update(values)
        if self.mtime == 0 and 'mtime' in values:
            self.mtime = values['mtime']

    def get_album(self):
        """Get the Album object that this item belongs to, if any, or
        None if the item is a singleton or is not associated with a
        library.
        """
        if not self._lib:
            return None
        return self._lib.get_album(self)


    # Interaction with file metadata.

    def read(self, read_path=None):
        """Read the metadata from the associated file. If read_path is
        specified, read metadata from that file instead.
        """
        if read_path is None:
            read_path = self.path
        else:
            read_path = normpath(read_path)
        try:
            f = MediaFile(syspath(read_path))
        except (OSError, IOError) as exc:
            raise util.FilesystemError(exc, 'read', (read_path,),
                                       traceback.format_exc())

        for key in ITEM_KEYS_META:
            value = getattr(f, key)
            if isinstance(value, (int, long)):
                # Filter values wider than 64 bits (in signed
                # representation). SQLite cannot store them.
                # py26: Post transition, we can use:
                # value.bit_length() > 63
                if abs(value) >= 2 ** 63:
                    value = 0
            setattr(self, key, value)

        # Database's mtime should now reflect the on-disk value.
        if read_path == self.path:
            self.mtime = self.current_mtime()

        self.path = read_path

    def write(self):
        """Writes the item's metadata to the associated file.
        """
        plugins.send('write', item=self)

        try:
            f = MediaFile(syspath(self.path))
        except (OSError, IOError) as exc:
            raise util.FilesystemError(exc, 'read', (self.path,),
                                       traceback.format_exc())

        for key in ITEM_KEYS_WRITABLE:
            setattr(f, key, self[key])

        try:
            f.save(id3v23=beets.config['id3v23'].get(bool))
        except (OSError, IOError) as exc:
            raise util.FilesystemError(exc, 'write', (self.path,),
                                       traceback.format_exc())

        # The file has a new mtime.
        self.mtime = self.current_mtime()


    # Files themselves.

    def move_file(self, dest, copy=False):
        """Moves or copies the item's file, updating the path value if
        the move succeeds. If a file exists at ``dest``, then it is
        slightly modified to be unique.
        """
        if not util.samefile(self.path, dest):
            dest = util.unique_path(dest)
        if copy:
            util.copy(self.path, dest)
        else:
            util.move(self.path, dest)
            plugins.send("item_moved", item=self, source=self.path,
                         destination=dest)

        # Either copying or moving succeeded, so update the stored path.
        self.path = dest

    def current_mtime(self):
        """Returns the current mtime of the file, rounded to the nearest
        integer.
        """
        return int(os.path.getmtime(syspath(self.path)))


    # Model methods.

    def remove(self, delete=False, with_album=True):
        """Removes the item. If `delete`, then the associated file is
        removed from disk. If `with_album`, then the item's album (if
        any) is removed if it the item was the last in the album.
        """
        super(Item, self).remove()

        # Remove the album if it is empty.
        if with_album:
            album = self.get_album()
            if album and not album.items():
                album.remove(delete, False)

        # Delete the associated file.
        if delete:
            util.remove(self.path)
            util.prune_dirs(os.path.dirname(self.path), self._lib.directory)

        self._lib._memotable = {}

    def move(self, copy=False, basedir=None, with_album=True):
        """Move the item to its designated location within the library
        directory (provided by destination()). Subdirectories are
        created as needed. If the operation succeeds, the item's path
        field is updated to reflect the new location.

        If copy is True, moving the file is copied rather than moved.

        basedir overrides the library base directory for the
        destination.

        If the item is in an album, the album is given an opportunity to
        move its art. (This can be disabled by passing
        with_album=False.)

        The item is stored to the database if it is in the database, so
        any dirty fields prior to the move() call will be written as a
        side effect. You probably want to call save() to commit the DB
        transaction.
        """
        self._check_db()
        dest = self.destination(basedir=basedir)

        # Create necessary ancestry for the move.
        util.mkdirall(dest)

        # Perform the move and store the change.
        old_path = self.path
        self.move_file(dest, copy)
        self.store()

        # If this item is in an album, move its art.
        if with_album:
            album = self.get_album()
            if album:
                album.move_art(copy)
                album.store()

        # Prune vacated directory.
        if not copy:
            util.prune_dirs(os.path.dirname(old_path), self._lib.directory)


    # Templating.

    def _formatted_mapping(self, for_path=False):
        """Get a mapping containing string-formatted values from either
        this item or the associated album, if any.
        """
        mapping = super(Item, self)._formatted_mapping(for_path)

        # Merge in album-level fields.
        album = self.get_album()
        if album:
            for key in album.keys(True):
                if key in ALBUM_KEYS_ITEM or key not in ITEM_KEYS:
                    mapping[key] = album._get_formatted(key, for_path)

        # Use the album artist if the track artist is not set and
        # vice-versa.
        if not mapping['artist']:
            mapping['artist'] = mapping['albumartist']
        if not mapping['albumartist']:
            mapping['albumartist'] = mapping['artist']

        return mapping

    def destination(self, fragment=False, basedir=None, platform=None,
                    path_formats=None):
        """Returns the path in the library directory designated for the
        item (i.e., where the file ought to be). fragment makes this
        method return just the path fragment underneath the root library
        directory; the path is also returned as Unicode instead of
        encoded as a bytestring. basedir can override the library's base
        directory for the destination.
        """
        self._check_db()
        platform = platform or sys.platform
        basedir = basedir or self._lib.directory
        path_formats = path_formats or self._lib.path_formats

        # Use a path format based on a query, falling back on the
        # default.
        for query, path_format in path_formats:
            if query == PF_KEY_DEFAULT:
                continue
            query = AndQuery.from_string(query)
            if query.match(self):
                # The query matches the item! Use the corresponding path
                # format.
                break
        else:
            # No query matched; fall back to default.
            for query, path_format in path_formats:
                if query == PF_KEY_DEFAULT:
                    break
            else:
                assert False, "no default path format"
        if isinstance(path_format, Template):
            subpath_tmpl = path_format
        else:
            subpath_tmpl = Template(path_format)

        # Evaluate the selected template.
        subpath = self.evaluate_template(subpath_tmpl, True)

        # Prepare path for output: normalize Unicode characters.
        if platform == 'darwin':
            subpath = unicodedata.normalize('NFD', subpath)
        else:
            subpath = unicodedata.normalize('NFC', subpath)
        # Truncate components and remove forbidden characters.
        subpath = util.sanitize_path(subpath, self._lib.replacements)
        # Encode for the filesystem.
        if not fragment:
            subpath = bytestring_path(subpath)

        # Preserve extension.
        _, extension = os.path.splitext(self.path)
        if fragment:
            # Outputting Unicode.
            extension = extension.decode('utf8', 'ignore')
        subpath += extension.lower()

        # Truncate too-long components.
        maxlen = beets.config['max_filename_length'].get(int)
        if not maxlen:
            # When zero, try to determine from filesystem.
            maxlen = util.max_filename_length(self._lib.directory)
        subpath = util.truncate_path(subpath, maxlen)

        if fragment:
            return subpath
        else:
            return normpath(os.path.join(basedir, subpath))


class Album(Model):
    """Provides access to information about albums stored in a
    library. Reflects the library's "albums" table, including album
    art.
    """
    _fields = ALBUM_KEYS
    _table = 'albums'
    _flex_table = 'album_attributes'
    _search_fields = ALBUM_DEFAULT_FIELDS

    @classmethod
    def _getters(cls):
        # In addition to plugin-provided computed fields, also expose
        # the album's directory as `path`.
        getters = plugins.album_field_getters()
        getters['path'] = Album.item_dir
        return getters

    def __setitem__(self, key, value):
        """Set the value of an album attribute."""
        if key == 'artpath':
            if isinstance(value, unicode):
                value = bytestring_path(value)
            elif isinstance(value, buffer):
                value = bytes(value)
        super(Album, self).__setitem__(key, value)

    def items(self):
        """Returns an iterable over the items associated with this
        album.
        """
        return self._lib.items(MatchQuery('album_id', self.id))

    def remove(self, delete=False, with_items=True):
        """Removes this album and all its associated items from the
        library. If delete, then the items' files are also deleted
        from disk, along with any album art. The directories
        containing the album are also removed (recursively) if empty.
        Set with_items to False to avoid removing the album's items.
        """
        super(Album, self).remove()

        # Delete art file.
        if delete:
            artpath = self.artpath
            if artpath:
                util.remove(artpath)

        # Remove (and possibly delete) the constituent items.
        if with_items:
            for item in self.items():
                item.remove(delete, False)

    def move_art(self, copy=False):
        """Move or copy any existing album art so that it remains in the
        same directory as the items.
        """
        old_art = self.artpath
        if not old_art:
            return

        new_art = self.art_destination(old_art)
        if new_art == old_art:
            return

        new_art = util.unique_path(new_art)
        log.debug('moving album art %s to %s' % (old_art, new_art))
        if copy:
            util.copy(old_art, new_art)
        else:
            util.move(old_art, new_art)
        self.artpath = new_art

        # Prune old path when moving.
        if not copy:
            util.prune_dirs(os.path.dirname(old_art),
                            self._lib.directory)

    def move(self, copy=False, basedir=None):
        """Moves (or copies) all items to their destination. Any album
        art moves along with them. basedir overrides the library base
        directory for the destination. The album is stored to the
        database, persisting any modifications to its metadata.
        """
        basedir = basedir or self._lib.directory

        # Ensure new metadata is available to items for destination
        # computation.
        self.store()

        # Move items.
        items = list(self.items())
        for item in items:
            item.move(copy, basedir=basedir, with_album=False)

        # Move art.
        self.move_art(copy)
        self.store()

    def item_dir(self):
        """Returns the directory containing the album's first item,
        provided that such an item exists.
        """
        item = self.items().get()
        if not item:
            raise ValueError('empty album')
        return os.path.dirname(item.path)

    def art_destination(self, image, item_dir=None):
        """Returns a path to the destination for the album art image
        for the album. `image` is the path of the image that will be
        moved there (used for its extension).

        The path construction uses the existing path of the album's
        items, so the album must contain at least one item or
        item_dir must be provided.
        """
        image = bytestring_path(image)
        item_dir = item_dir or self.item_dir()

        filename_tmpl = Template(beets.config['art_filename'].get(unicode))
        subpath = self.evaluate_template(filename_tmpl, True)
        subpath = util.sanitize_path(subpath,
                                     replacements=self._lib.replacements)
        subpath = bytestring_path(subpath)

        _, ext = os.path.splitext(image)
        dest = os.path.join(item_dir, subpath + ext)

        return bytestring_path(dest)

    def set_art(self, path, copy=True):
        """Sets the album's cover art to the image at the given path.
        The image is copied (or moved) into place, replacing any
        existing art.
        """
        path = bytestring_path(path)
        oldart = self.artpath
        artdest = self.art_destination(path)

        if oldart and samefile(path, oldart):
            # Art already set.
            return
        elif samefile(path, artdest):
            # Art already in place.
            self.artpath = path
            return

        # Normal operation.
        if oldart == artdest:
            util.remove(oldart)
        artdest = util.unique_path(artdest)
        if copy:
            util.copy(path, artdest)
        else:
            util.move(path, artdest)
        self.artpath = artdest

    def store(self):
        """Update the database with the album information. The album's
        tracks are also updated.
        """
        # Get modified track fields.
        track_updates = {}
        for key in ALBUM_KEYS_ITEM:
            if key in self._dirty:
                track_updates[key] = self[key]

        with self._lib.transaction():
            super(Album, self).store()
            if track_updates:
                for item in self.items():
                    for key, value in track_updates.items():
                        item[key] = value
                    item.store()



# Query abstraction hierarchy.


class Query(object):
    """An abstract class representing a query into the item database.
    """
    def clause(self):
        """Generate an SQLite expression implementing the query.
        Return a clause string, a sequence of substitution values for
        the clause, and a Query object representing the "remainder"
        Returns (clause, subvals) where clause is a valid sqlite
        WHERE clause implementing the query and subvals is a list of
        items to be substituted for ?s in the clause.
        """
        return None, ()

    def match(self, item):
        """Check whether this query matches a given Item. Can be used to
        perform queries on arbitrary sets of Items.
        """
        raise NotImplementedError


class FieldQuery(Query):
    """An abstract query that searches in a specific field for a
    pattern. Subclasses must provide a `value_match` class method, which
    determines whether a certain pattern string matches a certain value
    string. Subclasses may also provide `col_clause` to implement the
    same matching functionality in SQLite.
    """
    def __init__(self, field, pattern, fast=True):
        self.field = field
        self.pattern = pattern
        self.fast = fast

    def col_clause(self):
        return None, ()

    def clause(self):
        if self.fast:
            return self.col_clause()
        else:
            # Matching a flexattr. This is a slow query.
            return None, ()

    @classmethod
    def value_match(cls, pattern, value):
        """Determine whether the value matches the pattern. Both
        arguments are strings.
        """
        raise NotImplementedError()

    @classmethod
    def _raw_value_match(cls, pattern, value):
        """Determine whether the value matches the pattern. The value
        may have any type.
        """
        return cls.value_match(pattern, util.as_string(value))

    def match(self, item):
        return self._raw_value_match(self.pattern, item.get(self.field))


class MatchQuery(FieldQuery):
    """A query that looks for exact matches in an item field."""
    def col_clause(self):
        pattern = self.pattern
        if self.field == 'path':
            pattern = buffer(bytestring_path(pattern))
        return self.field + " = ?", [pattern]

    # We override the "raw" version here as a special case because we
    # want to compare objects before conversion.
    @classmethod
    def _raw_value_match(cls, pattern, value):
        return pattern == value


class SubstringQuery(FieldQuery):
    """A query that matches a substring in a specific item field."""
    def col_clause(self):
        search = '%' + (self.pattern.replace('\\','\\\\').replace('%','\\%')
                            .replace('_','\\_')) + '%'
        clause = self.field + " like ? escape '\\'"
        subvals = [search]
        return clause, subvals

    @classmethod
    def value_match(cls, pattern, value):
        return pattern.lower() in value.lower()


class RegexpQuery(FieldQuery):
    """A query that matches a regular expression in a specific item
    field.
    """
    @classmethod
    def value_match(cls, pattern, value):
        try:
            res = re.search(pattern, value)
        except re.error:
            # Invalid regular expression.
            return False
        return res is not None


class BooleanQuery(MatchQuery):
    """Matches a boolean field. Pattern should either be a boolean or a
    string reflecting a boolean.
    """
    def __init__(self, field, pattern):
        super(BooleanQuery, self).__init__(field, pattern)
        if isinstance(pattern, basestring):
            self.pattern = util.str2bool(pattern)
        self.pattern = int(self.pattern)


class NumericQuery(FieldQuery):
    """Matches numeric fields. A syntax using Ruby-style range ellipses
    (``..``) lets users specify one- or two-sided ranges. For example,
    ``year:2001..`` finds music released since the turn of the century.
    """
    kinds = dict((r[0], r[1]) for r in ITEM_FIELDS)

    @classmethod
    def applies_to(cls, field):
        """Determine whether a field has numeric type. NumericQuery
        should only be used with such fields.
        """
        return cls.kinds.get(field) in (int, float)

    def _convert(self, s):
        """Convert a string to the appropriate numeric type. If the
        string cannot be converted, return None.
        """
        try:
            return self.numtype(s)
        except ValueError:
            return None

    def __init__(self, field, pattern, fast=True):
        super(NumericQuery, self).__init__(field, pattern, fast)
        self.numtype = self.kinds[field]

        parts = pattern.split('..', 1)
        if len(parts) == 1:
            # No range.
            self.point = self._convert(parts[0])
            self.rangemin = None
            self.rangemax = None
        else:
            # One- or two-sided range.
            self.point = None
            self.rangemin = self._convert(parts[0])
            self.rangemax = self._convert(parts[1])

    def match(self, item):
        value = getattr(item, self.field)
        if isinstance(value, basestring):
            value = self._convert(value)

        if self.point is not None:
            return value == self.point
        else:
            if self.rangemin is not None and value < self.rangemin:
                return False
            if self.rangemax is not None and value > self.rangemax:
                return False
            return True

    def col_clause(self):
        if self.point is not None:
            return self.field + '=?', (self.point,)
        else:
            if self.rangemin is not None and self.rangemax is not None:
                return (u'{0} >= ? AND {0} <= ?'.format(self.field),
                        (self.rangemin, self.rangemax))
            elif self.rangemin is not None:
                return u'{0} >= ?'.format(self.field), (self.rangemin,)
            elif self.rangemax is not None:
                return u'{0} <= ?'.format(self.field), (self.rangemax,)
            else:
                return '1'


class SingletonQuery(Query):
    """Matches either singleton or non-singleton items."""
    def __init__(self, sense):
        self.sense = sense

    def clause(self):
        if self.sense:
            return "album_id ISNULL", ()
        else:
            return "NOT album_id ISNULL", ()

    def match(self, item):
        return (not item.album_id) == self.sense


class CollectionQuery(Query):
    """An abstract query class that aggregates other queries. Can be
    indexed like a list to access the sub-queries.
    """
    def __init__(self, subqueries=()):
        self.subqueries = subqueries

    # is there a better way to do this?
    def __len__(self): return len(self.subqueries)
    def __getitem__(self, key): return self.subqueries[key]
    def __iter__(self): return iter(self.subqueries)
    def __contains__(self, item): return item in self.subqueries

    def clause_with_joiner(self, joiner):
        """Returns a clause created by joining together the clauses of
        all subqueries with the string joiner (padded by spaces).
        """
        clause_parts = []
        subvals = []
        for subq in self.subqueries:
            subq_clause, subq_subvals = subq.clause()
            if not subq_clause:
                # Fall back to slow query.
                return None, ()
            clause_parts.append('(' + subq_clause + ')')
            subvals += subq_subvals
        clause = (' ' + joiner + ' ').join(clause_parts)
        return clause, subvals

    @classmethod
    def from_strings(cls, query_parts, default_fields, all_keys):
        """Creates a query from a list of strings in the format used by
        parse_query_part. If default_fields are specified, they are the
        fields to be searched by unqualified search terms. Otherwise,
        all fields are searched for those terms.
        """
        subqueries = []
        for part in query_parts:
            subq = construct_query_part(part, default_fields, all_keys)
            if subq:
                subqueries.append(subq)
        if not subqueries:  # No terms in query.
            subqueries = [TrueQuery()]
        return cls(subqueries)

    @classmethod
    def from_string(cls, query, default_fields=ITEM_DEFAULT_FIELDS,
                    all_keys=ITEM_KEYS):
        """Creates a query based on a single string. The string is split
        into query parts using shell-style syntax.
        """
        # A bug in Python < 2.7.3 prevents correct shlex splitting of
        # Unicode strings.
        # http://bugs.python.org/issue6988
        if isinstance(query, unicode):
            query = query.encode('utf8')
        parts = [s.decode('utf8') for s in shlex.split(query)]
        return cls.from_strings(parts, default_fields, all_keys)


class AnyFieldQuery(CollectionQuery):
    """A query that matches if a given FieldQuery subclass matches in
    any field. The individual field query class is provided to the
    constructor.
    """
    def __init__(self, pattern, fields, cls):
        self.pattern = pattern
        self.fields = fields
        self.query_class = cls

        subqueries = []
        for field in self.fields:
            subqueries.append(cls(field, pattern, True))
        super(AnyFieldQuery, self).__init__(subqueries)

    def clause(self):
        return self.clause_with_joiner('or')

    def match(self, item):
        for subq in self.subqueries:
            if subq.match(item):
                return True
        return False


class MutableCollectionQuery(CollectionQuery):
    """A collection query whose subqueries may be modified after the
    query is initialized.
    """
    def __setitem__(self, key, value):
        self.subqueries[key] = value

    def __delitem__(self, key):
        del self.subqueries[key]


class AndQuery(MutableCollectionQuery):
    """A conjunction of a list of other queries."""
    def clause(self):
        return self.clause_with_joiner('and')

    def match(self, item):
        return all([q.match(item) for q in self.subqueries])


class TrueQuery(Query):
    """A query that always matches."""
    def clause(self):
        return '1', ()

    def match(self, item):
        return True


class FalseQuery(Query):
    """A query that never matches."""
    def clause(self):
        return '0', ()

    def match(self, item):
        return False


class PathQuery(Query):
    """A query that matches all items under a given path."""
    def __init__(self, path):
        # Match the path as a single file.
        self.file_path = bytestring_path(normpath(path))
        # As a directory (prefix).
        self.dir_path = bytestring_path(os.path.join(self.file_path, ''))

    def match(self, item):
        return (item.path == self.file_path) or \
               item.path.startswith(self.dir_path)

    def clause(self):
        dir_pat = buffer(self.dir_path + '%')
        file_blob = buffer(self.file_path)
        return '(path = ?) || (path LIKE ?)', (file_blob, dir_pat)



# Query construction and parsing helpers.


PARSE_QUERY_PART_REGEX = re.compile(
    # Non-capturing optional segment for the keyword.
    r'(?:'
        r'(\S+?)'    # The field key.
        r'(?<!\\):'  # Unescaped :
    r')?'

    r'(.+)',         # The term itself.

    re.I  # Case-insensitive.
)

def parse_query_part(part):
    """Takes a query in the form of a key/value pair separated by a
    colon. The value part is matched against a list of prefixes that
    can be extended by plugins to add custom query types. For
    example, the colon prefix denotes a regular expression query.

    The function returns a tuple of `(key, value, cls)`. `key` may
    be None, indicating that any field may be matched. `cls` is a
    subclass of `FieldQuery`.

    For instance,
    parse_query('stapler') == (None, 'stapler', None)
    parse_query('color:red') == ('color', 'red', None)
    parse_query(':^Quiet') == (None, '^Quiet', RegexpQuery)
    parse_query('color::b..e') == ('color', 'b..e', RegexpQuery)

    Prefixes may be 'escaped' with a backslash to disable the keying
    behavior.
    """
    part = part.strip()
    match = PARSE_QUERY_PART_REGEX.match(part)

    prefixes = {':': RegexpQuery}
    prefixes.update(plugins.queries())

    if match:
        key = match.group(1)
        term = match.group(2).replace('\:', ':')
        # Match the search term against the list of prefixes.
        for pre, query_class in prefixes.items():
            if term.startswith(pre):
                return key, term[len(pre):], query_class
        if key and NumericQuery.applies_to(key):
            return key, term, NumericQuery
        return key, term, SubstringQuery  # The default query type.


def construct_query_part(query_part, default_fields, all_keys):
    """Create a query from a single query component. Return a Query
    instance or None if the value cannot be parsed.
    """
    parsed = parse_query_part(query_part)
    if not parsed:
        return

    key, pattern, query_class = parsed

    # No key specified.
    if key is None:
        if os.sep in pattern and 'path' in all_keys:
            # This looks like a path.
            return PathQuery(pattern)
        elif issubclass(query_class, FieldQuery):
            # The query type matches a specific field, but none was
            # specified. So we use a version of the query that matches
            # any field.
            return AnyFieldQuery(pattern, default_fields, query_class)
        else:
            # Other query type.
            return query_class(pattern)

    key = key.lower()

    # A boolean field.
    if key.lower() == 'comp':
        return BooleanQuery(key, pattern)

    # Path field.
    elif key == 'path' and 'path' in all_keys:
        if query_class is SubstringQuery:
            # By default, use special path matching logic.
            return PathQuery(pattern)
        else:
            # Specific query type requested.
            return query_class('path', pattern)

    # Singleton query (not a real field).
    elif key == 'singleton':
        return SingletonQuery(util.str2bool(pattern))

    # Other field.
    else:
        return query_class(key.lower(), pattern, key in all_keys)


def get_query(val, model_cls):
    """Takes a value which may be None, a query string, a query string
    list, or a Query object, and returns a suitable Query object.
    `model_cls` is the subclass of Model indicating which entity this
    is a query for (i.e., Album or Item) and is used to determine which
    fields are searched.
    """
    # Convert a single string into a list of space-separated
    # criteria.
    if isinstance(val, basestring):
        val = val.split()

    if val is None:
        return TrueQuery()
    elif isinstance(val, list) or isinstance(val, tuple):
        return AndQuery.from_strings(val, model_cls._search_fields,
                                     model_cls._fields)
    elif isinstance(val, Query):
        return val
    else:
        raise ValueError('query must be None or have type Query or str')



# The Library: interface to the database.


class Results(object):
    """An item query result set. Iterating over the collection lazily
    constructs LibModel objects that reflect database rows.
    """
    def __init__(self, model_class, rows, lib, query=None):
        """Create a result set that will construct objects of type
        `model_class`, which should be a subclass of `LibModel`, out of
        the query result mapping in `rows`. The new objects are
        associated with the library `lib`. If `query` is provided, it is
        used as a predicate to filter the results for a "slow query" that
        cannot be evaluated by the database directly.
        """
        self.model_class = model_class
        self.rows = rows
        self.lib = lib
        self.query = query

    def __iter__(self):
        """Construct Python objects for all rows that pass the query
        predicate.
        """
        for row in self.rows:
            # Get the flexible attributes for the object.
            with self.lib.transaction() as tx:
                flex_rows = tx.query(
                    'SELECT * FROM {0} WHERE entity_id=?'.format(
                        self.model_class._flex_table
                    ),
                    (row['id'],)
                )
            values = dict(row)
            values.update(
                dict((row['key'], row['value']) for row in flex_rows)
            )

            # Construct the Python object and yield it if it passes the
            # predicate.
            obj = self.model_class(self.lib, **values)
            if not self.query or self.query.match(obj):
                yield obj

    def __len__(self):
        """Get the number of matching objects.
        """
        if self.query:
            # A slow query. Fall back to testing every object.
            count = 0
            for obj in self:
                count += 1
            return count

        else:
            # A fast query. Just count the rows.
            return len(self.rows)

    def __nonzero__(self):
        """Does this result contain any objects?
        """
        return bool(len(self))

    def __getitem__(self, n):
        """Get the nth item in this result set. This is inefficient: all
        items up to n are materialized and thrown away.
        """
        it = iter(self)
        try:
            for i in range(n):
                it.next()
            return it.next()
        except StopIteration:
            raise IndexError('result index {0} out of range'.format(n))

    def get(self):
        """Return the first matching object, or None if no objects
        match.
        """
        it = iter(self)
        try:
            return it.next()
        except StopIteration:
            return None


class Transaction(object):
    """A context manager for safe, concurrent access to the database.
    All SQL commands should be executed through a transaction.
    """
    def __init__(self, lib):
        self.lib = lib

    def __enter__(self):
        """Begin a transaction. This transaction may be created while
        another is active in a different thread.
        """
        with self.lib._tx_stack() as stack:
            first = not stack
            stack.append(self)
        if first:
            # Beginning a "root" transaction, which corresponds to an
            # SQLite transaction.
            self.lib._db_lock.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Complete a transaction. This must be the most recently
        entered but not yet exited transaction. If it is the last active
        transaction, the database updates are committed.
        """
        with self.lib._tx_stack() as stack:
            assert stack.pop() is self
            empty = not stack
        if empty:
            # Ending a "root" transaction. End the SQLite transaction.
            self.lib._connection().commit()
            self.lib._db_lock.release()

    def query(self, statement, subvals=()):
        """Execute an SQL statement with substitution values and return
        a list of rows from the database.
        """
        cursor = self.lib._connection().execute(statement, subvals)
        return cursor.fetchall()

    def mutate(self, statement, subvals=()):
        """Execute an SQL statement with substitution values and return
        the row ID of the last affected row.
        """
        cursor = self.lib._connection().execute(statement, subvals)
        plugins.send('database_change', lib=self.lib)
        return cursor.lastrowid

    def script(self, statements):
        """Execute a string containing multiple SQL statements."""
        self.lib._connection().executescript(statements)


class Library(object):
    """A database of music containing songs and albums.
    """
    def __init__(self, path='library.blb',
                       directory='~/Music',
                       path_formats=((PF_KEY_DEFAULT,
                                      '$artist/$album/$track $title'),),
                       replacements=None,
                       item_fields=ITEM_FIELDS,
                       album_fields=ALBUM_FIELDS):
        if path == ':memory:':
            self.path = path
        else:
            self.path = bytestring_path(normpath(path))
        self.directory = bytestring_path(normpath(directory))
        self.path_formats = path_formats
        self.replacements = replacements

        self._memotable = {}  # Used for template substitution performance.

        self._connections = {}
        self._tx_stacks = defaultdict(list)
        # A lock to protect the _connections and _tx_stacks maps, which
        # both map thread IDs to private resources.
        self._shared_map_lock = threading.Lock()
        # A lock to protect access to the database itself. SQLite does
        # allow multiple threads to access the database at the same
        # time, but many users were experiencing crashes related to this
        # capability: where SQLite was compiled without HAVE_USLEEP, its
        # backoff algorithm in the case of contention was causing
        # whole-second sleeps (!) that would trigger its internal
        # timeout. Using this lock ensures only one SQLite transaction
        # is active at a time.
        self._db_lock = threading.Lock()

        # Set up database schema.
        self._make_table(Item._table, item_fields)
        self._make_table(Album._table, album_fields)
        self._make_attribute_table(Item._flex_table)
        self._make_attribute_table(Album._flex_table)

    def _make_table(self, table, fields):
        """Set up the schema of the library file. fields is a list of
        all the fields that should be present in the indicated table.
        Columns are added if necessary.
        """
        # Get current schema.
        with self.transaction() as tx:
            rows = tx.query('PRAGMA table_info(%s)' % table)
        current_fields = set([row[1] for row in rows])

        field_names = set([f[0] for f in fields])
        if current_fields.issuperset(field_names):
            # Table exists and has all the required columns.
            return

        if not current_fields:
            # No table exists.
            columns = []
            for field in fields:
                name, typ = field[:2]
                if name == 'id':
                    sql_type = SQLITE_KEY_TYPE
                else:
                    sql_type = SQLITE_TYPES[typ]
                columns.append('{0} {1}'.format(name, sql_type))
            setup_sql = 'CREATE TABLE {0} ({1});\n'.format(table,
                                                           ', '.join(columns))

        else:
            # Table exists but is missing fields.
            setup_sql = ''
            for fname in field_names - current_fields:
                for field in fields:
                    if field[0] == fname:
                        break
                else:
                    assert False
                setup_sql += 'ALTER TABLE {0} ADD COLUMN {1} {2};\n'.format(
                    table, field[0], SQLITE_TYPES[field[1]]
                )

        # Special case. If we're moving from a version without
        # albumartist, copy all the "artist" values to "albumartist"
        # values on the album data structure.
        if table == 'albums' and 'artist' in current_fields and \
                    'albumartist' not in current_fields:
            setup_sql += "UPDATE ALBUMS SET albumartist=artist;\n"

        with self.transaction() as tx:
            tx.script(setup_sql)

    def _make_attribute_table(self, flex_table):
        """Create a table and associated index for flexible attributes
        for the given entity (if they don't exist).
        """
        with self.transaction() as tx:
            tx.script("""
                CREATE TABLE IF NOT EXISTS {0} (
                    id INTEGER PRIMARY KEY,
                    entity_id INTEGER,
                    key TEXT,
                    value TEXT,
                    UNIQUE(entity_id, key) ON CONFLICT REPLACE);
                CREATE INDEX IF NOT EXISTS {0}_by_entity
                    ON {0} (entity_id);
                """.format(flex_table))

    def _connection(self):
        """Get a SQLite connection object to the underlying database.
        One connection object is created per thread.
        """
        thread_id = threading.current_thread().ident
        with self._shared_map_lock:
            if thread_id in self._connections:
                return self._connections[thread_id]
            else:
                # Make a new connection.
                conn = sqlite3.connect(
                    self.path,
                    timeout=beets.config['timeout'].as_number(),
                )

                # Access SELECT results like dictionaries.
                conn.row_factory = sqlite3.Row

                self._connections[thread_id] = conn
                return conn

    @contextlib.contextmanager
    def _tx_stack(self):
        """A context manager providing access to the current thread's
        transaction stack. The context manager synchronizes access to
        the stack map. Transactions should never migrate across threads.
        """
        thread_id = threading.current_thread().ident
        with self._shared_map_lock:
            yield self._tx_stacks[thread_id]

    def transaction(self):
        """Get a :class:`Transaction` object for interacting directly
        with the underlying SQLite database.
        """
        return Transaction(self)


    # Adding objects to the database.

    def add(self, obj):
        """Add the :class:`Item` or :class:`Album` object to the library
        database. Return the object's new id.
        """
        obj.add(self)
        self._memotable = {}
        return obj.id

    def add_album(self, items):
        """Create a new album consisting of a list of items. The items
        are added to the database if they don't yet have an ID. Return a
        new :class:`Album` object.
        """
        # Create the album structure using metadata from the first item.
        values = dict((key, items[0][key]) for key in ALBUM_KEYS_ITEM)
        album = Album(self, **values)

        # Add the album structure and set the items' album_id fields.
        # Store or add the items.
        with self.transaction():
            album.add(self)
            for item in items:
                item.album_id = album.id
                if item.id is None:
                    item.add(self)
                else:
                    item.store()

        return album


    # Querying.

    def _fetch(self, model_cls, query, order_by=None):
        """Fetch the objects of type `model_cls` matching the given
        query. The query may be given as a string, string sequence, a
        Query object, or None (to fetch everything). If provided,
        `order_by` is a SQLite ORDER BY clause for sorting.
        """
        query = get_query(query, model_cls)
        where, subvals = query.clause()

        sql = "SELECT * FROM {0} WHERE {1}".format(
            model_cls._table,
            where or '1',
        )
        if order_by:
            sql += " ORDER BY {0}".format(order_by)
        with self.transaction() as tx:
            rows = tx.query(sql, subvals)

        return Results(model_cls, rows, self, None if where else query)

    def albums(self, query=None):
        """Get a sorted list of :class:`Album` objects matching the
        given query.
        """
        order = '{0}, album'.format(
            _orelse("albumartist_sort", "albumartist")
        )
        return self._fetch(Album, query, order)

    def items(self, query=None):
        """Get a sorted list of :class:`Item` objects matching the given
        query.
        """
        order = '{0}, album'.format(
            _orelse("artist_sort", "artist")
        )
        return self._fetch(Item, query, order)


    # Convenience accessors.

    def _get(self, model_cls, id):
        """Get a LibModel object by its id or None if the id does not
        exist.
        """
        return self._fetch(model_cls, MatchQuery('id', id)).get()

    def get_item(self, id):
        """Fetch an :class:`Item` by its ID. Returns `None` if no match is
        found.
        """
        return self._get(Item, id)

    def get_album(self, item_or_id):
        """Given an album ID or an item associated with an album, return
        an :class:`Album` object for the album. If no such album exists,
        returns `None`.
        """
        if isinstance(item_or_id, int):
            album_id = item_or_id
        else:
            album_id = item_or_id.album_id
        if album_id is None:
            return None
        return self._get(Album, album_id)



# Default path template resources.


def _int_arg(s):
    """Convert a string argument to an integer for use in a template
    function.  May raise a ValueError.
    """
    return int(s.strip())


class DefaultTemplateFunctions(object):
    """A container class for the default functions provided to path
    templates. These functions are contained in an object to provide
    additional context to the functions -- specifically, the Item being
    evaluated.
    """
    _prefix = 'tmpl_'

    def __init__(self, item=None, lib=None):
        """Paramaterize the functions. If `item` or `lib` is None, then
        some functions (namely, ``aunique``) will always evaluate to the
        empty string.
        """
        self.item = item
        self.lib = lib

    def functions(self):
        """Returns a dictionary containing the functions defined in this
        object. The keys are function names (as exposed in templates)
        and the values are Python functions.
        """
        out = {}
        for key in self._func_names:
            out[key[len(self._prefix):]] = getattr(self, key)
        return out

    @staticmethod
    def tmpl_lower(s):
        """Convert a string to lower case."""
        return s.lower()

    @staticmethod
    def tmpl_upper(s):
        """Covert a string to upper case."""
        return s.upper()

    @staticmethod
    def tmpl_title(s):
        """Convert a string to title case."""
        return s.title()

    @staticmethod
    def tmpl_left(s, chars):
        """Get the leftmost characters of a string."""
        return s[0:_int_arg(chars)]

    @staticmethod
    def tmpl_right(s, chars):
        """Get the rightmost characters of a string."""
        return s[-_int_arg(chars):]

    @staticmethod
    def tmpl_if(condition, trueval, falseval=u''):
        """If ``condition`` is nonempty and nonzero, emit ``trueval``;
        otherwise, emit ``falseval`` (if provided).
        """
        try:
            condition = _int_arg(condition)
        except ValueError:
            condition = condition.strip()
        if condition:
            return trueval
        else:
            return falseval

    @staticmethod
    def tmpl_asciify(s):
        """Translate non-ASCII characters to their ASCII equivalents.
        """
        return unidecode(s)

    @staticmethod
    def tmpl_time(s, format):
        """Format a time value using `strftime`.
        """
        cur_fmt = beets.config['time_format'].get(unicode)
        return time.strftime(format, time.strptime(s, cur_fmt))

    def tmpl_aunique(self, keys=None, disam=None):
        """Generate a string that is guaranteed to be unique among all
        albums in the library who share the same set of keys. A fields
        from "disam" is used in the string if one is sufficient to
        disambiguate the albums. Otherwise, a fallback opaque value is
        used. Both "keys" and "disam" should be given as
        whitespace-separated lists of field names.
        """
        # Fast paths: no album, no item or library, or memoized value.
        if not self.item or not self.lib:
            return u''
        if self.item.album_id is None:
            return u''
        memokey = ('aunique', keys, disam, self.item.album_id)
        memoval = self.lib._memotable.get(memokey)
        if memoval is not None:
            return memoval

        keys = keys or 'albumartist album'
        disam = disam or 'albumtype year label catalognum albumdisambig'
        keys = keys.split()
        disam = disam.split()

        album = self.lib.get_album(self.item)
        if not album:
            # Do nothing for singletons.
            self.lib._memotable[memokey] = u''
            return u''

        # Find matching albums to disambiguate with.
        subqueries = []
        for key in keys:
            value = getattr(album, key)
            subqueries.append(MatchQuery(key, value))
        albums = self.lib.albums(AndQuery(subqueries))

        # If there's only one album to matching these details, then do
        # nothing.
        if len(albums) == 1:
            self.lib._memotable[memokey] = u''
            return u''

        # Find the first disambiguator that distinguishes the albums.
        for disambiguator in disam:
            # Get the value for each album for the current field.
            disam_values = set([getattr(a, disambiguator) for a in albums])

            # If the set of unique values is equal to the number of
            # albums in the disambiguation set, we're done -- this is
            # sufficient disambiguation.
            if len(disam_values) == len(albums):
                break

        else:
            # No disambiguator distinguished all fields.
            res = u' {0}'.format(album.id)
            self.lib._memotable[memokey] = res
            return res

        # Flatten disambiguation value into a string.
        disam_value = album._get_formatted(disambiguator, True)
        res = u' [{0}]'.format(disam_value)
        self.lib._memotable[memokey] = res
        return res


# Get the name of tmpl_* functions in the above class.
DefaultTemplateFunctions._func_names = \
    [s for s in dir(DefaultTemplateFunctions)
     if s.startswith(DefaultTemplateFunctions._prefix)]
