from pathlib import PurePosixPath


FILE_TYPES = ("photos", "videos", "music", "files")
EXTENSIONS = {
    "photos": frozenset("jpg jpeg jpe png gif webp bmp tif tiff heic heif avif raw dng nef cr2 cr3 arw raf orf rw2 pef srw svg ico psd".split()),
    "videos": frozenset("mp4 m4v mov avi mkv webm mpg mpeg mpe mts m2ts ts vob wmv flv 3gp 3g2 ogv mxf".split()),
    "music": frozenset("mp3 flac wav wave ogg oga opus m4a aac alac aiff aif wma ape mid midi amr ac3 mka".split()),
}


def file_type(meta):
    media = str(meta.get("media_type") or "").lower()
    mime = str(meta.get("mime_type") or "").lower().split(";", 1)[0]
    known = {"image": "photos", "video": "videos", "audio": "music"}
    if media in known:
        return known[media]
    for category, prefix in (("photos", "image"), ("videos", "video"), ("music", "audio")):
        if mime.startswith(prefix + "/"):
            return category
    extension = PurePosixPath(meta.get("name") or meta.get("path") or "").suffix.lower().lstrip(".")
    return next((category for category, extensions in EXTENSIONS.items() if extension in extensions), "files")


def preview_type(meta):
    return file_type(meta) in ("photos", "videos")


def selected_type(meta, settings):
    from .selection_rules import included
    return file_type(meta) in settings.get("file_types", FILE_TYPES) and included(meta,settings)
