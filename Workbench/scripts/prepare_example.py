"""Keep a source ZIP and extract one example subtree without overwriting files."""
import argparse
from pathlib import Path, PurePosixPath
import shutil
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--prefix", required=True, help="Example subtree inside ZIP, including trailing slash")
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if not args.prefix.endswith("/"):
        parser.error("prefix must end with /")
    args.destination.mkdir(parents=True, exist_ok=False)
    archive = args.destination / args.archive.name
    shutil.copy2(args.archive, archive)
    count = 0
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            if not info.filename.startswith(args.prefix) or info.is_dir():
                continue
            rel = PurePosixPath(info.filename[len(args.prefix):])
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"Unsafe ZIP member: {info.filename}")
            target = args.destination / "original" / "TxtInOut" / Path(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))
            count += 1
        top = args.prefix.split("/")[0]
        for filename, output in [("README.md", "README_upstream.md"), ("LICENSE", "LICENSE")]:
            name = top + "/" + filename
            if name in z.namelist():
                (args.destination / output).write_bytes(z.read(name))
        print(f"Extracted {count} files; ZIP comment: {z.comment.decode(errors='replace')}")
    if not count:
        raise ValueError("No files matched prefix")


if __name__ == "__main__":
    main()
