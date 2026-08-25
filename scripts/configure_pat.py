"""Store a NovelAI persistent token with Windows DPAPI."""

import argparse
import ctypes
import getpass
import os
from pathlib import Path


class DataBlob(ctypes.Structure):
    """Match the Windows DATA_BLOB structure used by DPAPI."""

    _fields_ = [
        ("size", ctypes.c_uint32),
        ("data", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def main() -> int:
    """Read a PAT without echoing it and save DPAPI-encrypted bytes.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        description="Securely configure the NovelAI PAT for this AstrBot plugin.",
    )
    parser.add_argument(
        "--astrbot-data-dir",
        type=Path,
        help="AstrBot data directory; inferred when run from data/plugins.",
    )
    args = parser.parse_args()
    if os.name != "nt":
        parser.error(
            "DPAPI is only available on Windows. Set NOVELAI_API_TOKEN in the "
            "AstrBot service environment on Linux or in containers."
        )

    if args.astrbot_data_dir is not None:
        astrbot_data_dir = args.astrbot_data_dir.expanduser().resolve()
    else:
        plugin_dir = Path(__file__).resolve().parents[1]
        plugins_dir = plugin_dir.parent
        if plugins_dir.name != "plugins" or plugins_dir.parent.name != "data":
            parser.error(
                "Cannot infer AstrBot data directory. Pass --astrbot-data-dir."
            )
        astrbot_data_dir = plugins_dir.parent

    token = getpass.getpass("NovelAI persistent API token: ").strip()
    if token and not token.startswith("pst-"):
        token = f"pst-{token}"
    if len(token) < 16 or any(character.isspace() for character in token):
        parser.error("The PAT format is invalid.")

    plaintext = bytearray(token.encode("utf-8"))
    input_buffer = (ctypes.c_ubyte * len(plaintext)).from_buffer(plaintext)
    input_blob = DataBlob(
        len(plaintext),
        ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    output_blob = DataBlob()
    try:
        succeeded = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "AstrBot NovelAI PAT",
            None,
            None,
            None,
            1,
            ctypes.byref(output_blob),
        )
        if not succeeded:
            raise ctypes.WinError()
        encrypted = ctypes.string_at(output_blob.data, output_blob.size)
    finally:
        for index in range(len(plaintext)):
            plaintext[index] = 0
        if output_blob.data:
            ctypes.windll.kernel32.LocalFree(output_blob.data)

    output_dir = astrbot_data_dir / "plugin_data" / "astrbot_plugin_n5"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "novelai_pat.dpapi"
    temporary_path = output_path.with_suffix(".tmp")
    temporary_path.write_bytes(encrypted)
    temporary_path.replace(output_path)
    output_path.chmod(0o600)
    print(f"NovelAI PAT stored at: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
