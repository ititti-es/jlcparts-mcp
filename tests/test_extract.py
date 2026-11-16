import os
import zipfile

import pytest

from jlcparts_mcp import db


def make_zip(tmp_path, payload):
    path = tmp_path / "cache.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("cache.sqlite3", payload)
    return path


def test_extracts_a_plain_zip(tmp_path):
    payload = os.urandom(50_000) + b"A" * 200_000
    out = tmp_path / "out"
    db._extract([make_zip(tmp_path, payload)], out)
    assert out.read_bytes() == payload


def test_extracts_a_spanned_archive(tmp_path):
    payload = os.urandom(50_000) + b"B" * 200_000
    data = b"PK\x07\x08" + make_zip(tmp_path, payload).read_bytes()      # spanned archives start with this marker
    parts = [tmp_path / "cache.z01", tmp_path / "cache.z02", tmp_path / "cache.zip"]
    cut = len(data) // 3
    for p, chunk in zip(parts, (data[:cut], data[cut:2 * cut], data[2 * cut:])):
        p.write_bytes(chunk)
    out = tmp_path / "out"
    db._extract(parts, out)
    assert out.read_bytes() == payload


def test_rejects_a_mixed_or_truncated_download(tmp_path):
    data = make_zip(tmp_path, os.urandom(100_000)).read_bytes()
    first, last = tmp_path / "cache.z01", tmp_path / "cache.zip"
    first.write_bytes(data[:40_000])
    last.write_bytes(data[60_000:])                                      # a chunk is missing in the middle
    with pytest.raises(db.ArchiveError):
        db._extract([first, last], tmp_path / "out")
