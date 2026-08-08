#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# k4mobidedrm.py
# Copyright © 2008-2020 by Apprentice Harper et al.

__license__ = 'GPL v3'
__version__ = '6.0'

# Engine to remove drm from Kindle and Mobipocket ebooks
# for personal use for archiving and converting your ebooks

# PLEASE DO NOT PIRATE EBOOKS!

# We want all authors and publishers, and ebook stores to live
# long and prosperous lives but at the same time  we just want to
# be able to read OUR books on whatever device we want and to keep
# readable for a long, long time

# This borrows very heavily from works by CMBDTC, IHeartCabbages, skindle,
#    unswindle, DarkReverser, ApprenticeAlf, and many many others

# Special thanks to The Dark Reverser for MobiDeDrm and CMBDTC for cmbdtc_dump
# from which this script borrows most unashamedly.

# Changelog
#  1.0 - Name change to k4mobidedrm. Adds Mac support, Adds plugin code
#  ...
#  6.0 - Added Python 3 compatibility for calibre 5.0


import sys
import os
import re
import getopt
import traceback
import time
import html.entities
import json
import argparse
import logging
from pathlib import Path

class DrmException(Exception):
    pass

if 'calibre' in sys.modules:
    inCalibre = True
else:
    inCalibre = False

if inCalibre:
    from calibre_plugins.dedrm import mobidedrm
    from calibre_plugins.dedrm import topazextract
    from calibre_plugins.dedrm import kgenpids
    from calibre_plugins.dedrm import androidkindlekey
    from calibre_plugins.dedrm import kfxdedrm
else:
    import mobidedrm
    import topazextract
    import kgenpids
    import androidkindlekey
    import kfxdedrm

# Wrap a stream so that output gets flushed immediately
# and also make sure that any unicode strings get
# encoded using "replace" before writing them.
class SafeUnbuffered:
    def __init__(self, stream):
        self.stream = stream
        enc = getattr(stream, 'encoding', None)
        self.encoding = enc or 'utf-8'

    def write(self, data):
        # Accept bytes or str. Prefer writing text via stream.write.
        try:
            if isinstance(data, bytes):
                # decode bytes to text using the stream encoding, fall back gracefully
                try:
                    text = data.decode(self.encoding)
                except Exception:
                    text = data.decode('utf-8', 'replace')
                self.stream.write(text)
            else:
                # assume text
                self.stream.write(str(data))
        except Exception:
            # fallback: if the underlying stream exposes a buffer, write bytes there
            buf = getattr(self.stream, 'buffer', None)
            try:
                if buf is not None:
                    if isinstance(data, str):
                        buf.write(data.encode(self.encoding, 'replace'))
                    else:
                        buf.write(data)
            except Exception:
                # give up silently to avoid masking original errors during logging
                pass
        # try to flush whichever flushing method is available
        try:
            self.stream.flush()
        except Exception:
            try:
                buf = getattr(self.stream, 'buffer', None)
                if buf is not None:
                    buf.flush()
            except Exception:
                pass

    def __getattr__(self, attr):
        return getattr(self.stream, attr)

iswindows = sys.platform.startswith('win')
isosx = sys.platform.startswith('darwin')

def unicode_argv():
    if iswindows:
        # Uses shell32.GetCommandLineArgvW to get sys.argv as a list of Unicode
        # strings.

        from ctypes import POINTER, byref, cdll, c_int, windll
        from ctypes.wintypes import LPCWSTR, LPWSTR

        GetCommandLineW = cdll.kernel32.GetCommandLineW
        GetCommandLineW.argtypes = []
        GetCommandLineW.restype = LPCWSTR

        CommandLineToArgvW = windll.shell32.CommandLineToArgvW
        CommandLineToArgvW.argtypes = [LPCWSTR, POINTER(c_int)]
        CommandLineToArgvW.restype = POINTER(LPWSTR)

        cmd = GetCommandLineW()
        argc = c_int(0)
        argv = CommandLineToArgvW(cmd, byref(argc))
        if argc.value > 0:
            # Remove Python executable and commands if present
            start = argc.value - len(sys.argv)
            return [argv[i] for i in
                    range(start, argc.value)]
        return ["mobidedrm.py"]
    else:
        argvencoding = sys.stdin.encoding or "utf-8"
        return [arg if isinstance(arg, str) else str(arg, argvencoding) for arg in sys.argv]

# cleanup unicode filenames
# borrowed from calibre from calibre/src/calibre/__init__.py
# added in removal of control (<32) chars
# and removal of . at start and end
# and with some (heavily edited) code from Paul Durrant's kindlenamer.py
# and some improvements suggested by jhaisley
def cleanup_name(name):
    # substitute filename unfriendly characters
    name = name.replace("<","[").replace(">","]").replace(" : "," – ").replace(": "," – ").replace(":","—").replace("/","_").replace("\\","_").replace("|","_").replace('"',"'").replace("*","_")
    # white space to single space, delete leading and trailing while space
    name = re.sub(r"\s", " ", name).strip()
    # delete control characters
    name = "".join(char for char in name if ord(char)>=32)
    # delete non-ascii characters
    name = "".join(char for char in name if ord(char)<=126)
    # remove leading dots
    while len(name)>0 and name[0] == ".":
        name = name[1:]
    # remove trailing dots (Windows doesn't like them)
    while name.endswith("."):
        name = name[:-1]
    if len(name)==0:
        name="DecryptedBook"
    return name

# must be passed unicode
def unescape(text):
    def fixup(m):
        text = m.group(0)
        if text[:2] == "&#":
            # character reference
            try:
                if text[:3] == "&#x":
                    return chr(int(text[3:-1], 16))
                else:
                    return chr(int(text[2:-1]))
            except ValueError:
                pass
        else:
            # named entity
            try:
                text = chr(html.entities.name2codepoint[text[1:-1]])
            except KeyError:
                pass
        return text # leave as is
    return re.sub("&#?\\w+;", fixup, text)

def GetDecryptedBook(infile, kDatabases, androidFiles, serials, pids, starttime=None):
    # handle the obvious cases at the beginning
    if starttime is None:
        starttime = time.time()

    if not os.path.isfile(infile):
        raise DrmException("Input file does not exist.")

    mobi = True
    # read magic bytes with a context manager to avoid leaking file descriptors
    with open(infile, 'rb') as fh:
        magic8 = fh.read(8)

    if magic8 == b'\xeaDRMION\xee':
        raise DrmException("The .kfx DRMION file cannot be decrypted by itself. A .kfx-zip archive containing a DRM voucher is required.")

    magic3 = magic8[:3]
    if magic3 == b'TPZ':
        mobi = False

    # instantiate the appropriate book handler
    if magic8[:4] == b'PK\x03\x04':
        mb = kfxdedrm.KFXZipBook(infile)
    elif mobi:
        mb = mobidedrm.MobiBook(infile)
    else:
        mb = topazextract.TopazBook(infile)

    bookname = unescape(mb.getBookTitle())
    logging.info("Decrypting %s ebook: %s", mb.getBookType(), bookname)

    # copy list of pids
    totalpids = list(pids)
    # extend list of serials with serials from android databases
    for aFile in androidFiles:
        try:
            serials.extend(androidkindlekey.get_serials(aFile))
        except Exception:
            logging.exception("Error reading Android serials from %s", aFile)
    # extend PID list with book-specific PIDs from serials and kDatabases
    md1, md2 = mb.getPIDMetaInfo()
    try:
        totalpids.extend(kgenpids.getPidList(md1, md2, serials, kDatabases))
    except Exception:
        logging.exception("Error generating PID list for %s", infile)
    # remove any duplicates
    totalpids = list(set(totalpids))
    logging.info("Found %d keys to try after %.1f seconds", len(totalpids), time.time()-starttime)

    try:
        mb.processBook(totalpids)
    except Exception:
        # ensure cleanup is attempted when an error occurs
        try:
            if 'mb' in locals() and hasattr(mb, 'cleanup'):
                cleanup = getattr(mb, 'cleanup')
                if callable(cleanup):
                    cleanup()
        except Exception:
            logging.exception("Error while cleaning up after failed decryption")
        raise

    logging.info("Decryption succeeded after %.1f seconds", time.time()-starttime)
    return mb

# kDatabaseFiles is a list of files created by kindlekey
def decryptBook(infile, outdir, kDatabaseFiles, androidFiles, serials, pids):
    starttime = time.time()
    kDatabases = []
    for dbfile in kDatabaseFiles:
        kindleDatabase = {}
        try:
            with open(dbfile, 'r', encoding='utf-8') as keyfilein:
                kindleDatabase = json.load(keyfilein)
            kDatabases.append([dbfile, kindleDatabase])
        except Exception:
            logging.exception("Error getting database from file %s", dbfile)

    try:
        book = GetDecryptedBook(infile, kDatabases, androidFiles, serials, pids, starttime)
    except Exception as e:
        logging.exception("Error decrypting book after %.1f seconds: %s", time.time()-starttime, str(e))
        return 1

    # Try to infer a reasonable name
    orig_fn_root = os.path.splitext(os.path.basename(infile))[0]
    if (
        re.match(r'^B[A-Z0-9]{9}(_EBOK|_EBSP|_sample)?$', orig_fn_root) or
        re.match(r'^[0-9A-F-]{36}$', orig_fn_root)
    ):  # Kindle for PC / Mac / Android / Fire / iOS
        clean_title = cleanup_name(book.getBookTitle())
        outfilename = "{}_{}".format(orig_fn_root, clean_title)
    else:  # E Ink Kindle, which already uses a reasonable name
        outfilename = orig_fn_root

    # avoid excessively long file names
    if len(outfilename) > 150:
        outfilename = outfilename[:99] + "--" + outfilename[-49:]

    outfilename = outfilename + "_nodrm"

    # ensure outdir exists
    try:
        Path(outdir).mkdir(parents=True, exist_ok=True)
    except Exception:
        logging.exception("Unable to create output directory %s", outdir)
        return 1

    outfile = os.path.join(outdir, outfilename + book.getBookExtension())

    try:
        book.getFile(outfile)
        logging.info("Saved decrypted book %s after %.1f seconds", outfilename, time.time()-starttime)
    except Exception:
        logging.exception("Error saving decrypted book %s", outfilename)
        try:
            if hasattr(book, 'cleanup'):
                book.cleanup()
        except Exception:
            logging.exception("Error during cleanup after failed save")
        return 1

    if book.getBookType() == "Topaz":
        try:
            zipname = os.path.join(outdir, outfilename + "_SVG.zip")
            book.getSVGZip(zipname)
            logging.info("Saved SVG ZIP Archive for %s after %.1f seconds", outfilename, time.time()-starttime)
        except Exception:
            logging.exception("Error saving SVG ZIP Archive for %s", outfilename)

    # remove internal temporary directory of Topaz pieces
    try:
        if hasattr(book, 'cleanup'):
            book.cleanup()
    except Exception:
        logging.exception("Error during final cleanup for %s", outfilename)
    return 0


def usage(progname):
    print("Removes DRM protection from Mobipocket, Amazon KF8, Amazon Print Replica and Amazon Topaz ebooks")
    print("Usage:")
    print("    {0} [-k <kindle.k4i>] [-p <comma separated PIDs>] [-s <comma separated Kindle serial numbers>] [ -a <AmazonSecureStorage.xml|backup.ab> ] <infile> <outdir>".format(progname))

#
# Main
#
def cli_main():
    # prefer argparse for the CLI and configure logging
    argv = unicode_argv()
    progname = os.path.basename(argv[0])

    parser = argparse.ArgumentParser(prog=progname,
                                     description='Removes DRM protection from Mobipocket, Amazon KF8, Amazon Print Replica and Amazon Topaz ebooks')
    parser.add_argument('-k', '--keyfile', action='append', default=[], help='Kindle k4i key file (can be specified multiple times)')
    parser.add_argument('-p', '--pids', help='Comma separated PIDs')
    parser.add_argument('-s', '--serials', help='Comma separated Kindle serial numbers')
    parser.add_argument('-a', '--android', action='append', default=[], help='Android secure storage file(s) (can be specified multiple times)')
    parser.add_argument('infile')
    parser.add_argument('outdir')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')

    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format='%(levelname)s: %(message)s')

    infile = args.infile
    outdir = args.outdir
    kDatabaseFiles = args.keyfile
    androidFiles = args.android
    serials = [] if not args.serials else [s.strip() for s in args.serials.split(',') if s.strip()]
    pids = [] if not args.pids else [pid.strip() for pid in args.pids.split(',') if pid.strip()]

    return decryptBook(infile, outdir, kDatabaseFiles, androidFiles, serials, pids)


if __name__ == '__main__':
    sys.stdout = SafeUnbuffered(sys.stdout)
    sys.stderr = SafeUnbuffered(sys.stderr)
    sys.exit(cli_main())
