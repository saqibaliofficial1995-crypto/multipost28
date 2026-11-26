"""
Google Drive client with full pagination, download, and post-upload file removal.
"""
import socket

_orig = socket.getaddrinfo

def _ipv4(host, port, family=0, type=0, proto=0, flags=0):
    if family == 0 or family == socket.AF_UNSPEC:
        family = socket.AF_INET
    return _orig(host, port, family, type, proto, flags)

socket.getaddrinfo = _ipv4

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".avi", ".webm")

def get_drive_service(creds):
    return build("drive", "v3", credentials=creds)

def list_all_files_in_folder(service, folder_id):
    """
    List ALL files with full pagination (pageSize 100 + nextPageToken loop).
    Returns (video_files, non_conforming).
    """
    query = f"'{folder_id}' in parents and trashed = false"
    page_token = None
    all_items = []

    while True:
        resp = service.files().list(
            q=query,
            pageSize=100,
            fields="nextPageToken, files(id, name, mimeType, size, createdTime, parents, capabilities(canTrash,canDelete))",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageToken=page_token
        ).execute()

        files = resp.get("files", [])
        all_items.extend(files)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    video_files = []
    non_conforming = []

    for item in all_items:
        mime = item.get("mimeType", "")
        name = item.get("name", "")
        is_folder = (mime == "application/vnd.google-apps.folder")
        is_video = (mime.startswith("video/") or name.lower().endswith(VIDEO_EXTENSIONS)) and not is_folder

        if is_video:
            video_files.append(item)
        else:
            non_conforming.append(item)

    # Sort oldest first by createdTime/name for deterministic FIFO upload order
    video_files.sort(key=lambda x: (x.get("createdTime") or "", x.get("name") or ""))
    return video_files, non_conforming

def download_drive_file(service, file_id, dest_path):
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=1024*1024*8)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"[drive] Download progress: {int(status.progress() * 100)}%")

def remove_uploaded_file(service, file_id, source_folder_id):
    """
    Removes an uploaded video from the Drive inbox folder after confirmed YouTube publish.
    Tries moving the file to Drive Trash first (safe, frees folder); if the file is owned
    by another account (403 on trash), falls back to unlinking it from source_folder_id.
    """
    try:
        service.files().update(
            fileId=file_id,
            body={"trashed": True},
            supportsAllDrives=True,
            fields="id, trashed"
        ).execute()
        print(f"[drive] Moved uploaded file {file_id} to Trash.")
        return "trashed"
    except HttpError as e:
        if e.resp.status in (403, 404):
            try:
                service.files().update(
                    fileId=file_id,
                    removeParents=source_folder_id,
                    supportsAllDrives=True,
                    fields="id, parents"
                ).execute()
                print(f"[drive] Unlinked shared file {file_id} from folder {source_folder_id} (non-owner fallback).")
                return "unlinked"
            except Exception as inner_err:
                print(f"[drive] Warning: Could not remove shared file {file_id} from folder: {inner_err}")
                raise
        raise


def get_or_create_done_folder(service, source_folder_id, folder_name="Youtube Done"):
    """
    Finds or creates a 'Youtube Done' folder inside source_folder_id.
    """
    try:
        q = f"'{source_folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        resp = service.files().list(
            q=q,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            fields="files(id, name)"
        ).execute()
        files = resp.get("files", [])
        for f in files:
            if f.get("name", "").strip().lower() == folder_name.lower():
                return f["id"]

        meta = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [source_folder_id]
        }
        created = service.files().create(
            body=meta,
            supportsAllDrives=True,
            fields="id, name"
        ).execute()
        print(f"[drive] Created '{folder_name}' folder ({created['id']}) inside {source_folder_id}.")
        return created["id"]
    except Exception as e:
        print(f"[drive] Warning searching/creating '{folder_name}' folder: {e}")
        return None

def move_uploaded_file_to_done(service, file_id, source_folder_id, done_folder_name="Youtube Done"):
    """
    Moves uploaded video into 'Youtube Done' subfolder. Falls back to trash/unlink if move fails.
    """
    done_id = get_or_create_done_folder(service, source_folder_id, folder_name=done_folder_name)
    if done_id:
        try:
            service.files().update(
                fileId=file_id,
                addParents=done_id,
                removeParents=source_folder_id,
                supportsAllDrives=True,
                fields="id, parents"
            ).execute()
            print(f"[drive] Moved uploaded file {file_id} to '{done_folder_name}' folder.")
            return "moved_to_done"
        except Exception as e:
            print(f"[drive] Moving to '{done_folder_name}' failed ({e}), falling back to trash/unlink.")
    return remove_uploaded_file(service, file_id, source_folder_id)
