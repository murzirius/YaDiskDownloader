"""Create synthetic public fixtures for a dedicated read-only Disk test account."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import random
import wave
import zipfile

from PIL import Image


def create(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    image = Image.new('RGB', (640, 480), (28, 70, 110))
    for filename, fmt in [('photos/example.jpg', 'JPEG'), ('photos/Фото № 2.png', 'PNG')]:
        path = output / filename
        path.parent.mkdir(exist_ok=True)
        image.save(path, format=fmt)
    (output / 'documents').mkdir()
    (output / 'documents/readme.txt').write_text('Synthetic test data. No personal files.\n', encoding='utf-8')
    (output / 'documents/данные.json').write_text(json.dumps({'test': True, 'text': 'Проверка'}), encoding='utf-8')
    (output / 'empty-folder').mkdir()
    (output / 'music').mkdir()
    with wave.open(str(output / 'music/tone.wav'), 'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(8000)
        audio.writeframes(b'\0\0' * 8000)
    (output / 'nested').mkdir()
    for index in range(24):
        folder = output / 'nested' / ('group-%d' % (index // 8))
        folder.mkdir(exist_ok=True)
        (folder / ('file-%02d.txt' % index)).write_bytes(('Test file %d\n' % index).encode() * 100)
    block = random.Random(1500).randbytes(1024 * 1024)
    with (output / 'resume-test.bin').open('wb') as stream:
        for _ in range(16):
            stream.write(block)
    with zipfile.ZipFile(output / 'archive.zip', 'w') as archive:
        archive.writestr('example.txt', 'Synthetic archive content')
    files = []
    for path in sorted(output.rglob('*')):
        if path.is_file():
            with path.open('rb') as stream:
                sha = hashlib.file_digest(stream, 'sha256').hexdigest()
            files.append(dict(path=path.relative_to(output).as_posix(), size=path.stat().st_size, sha256=sha))
    manifest = {'format': 1, 'files': files}
    (output / 'fixture-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return len(files)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Создать безопасные тестовые файлы для отдельного аккаунта Яндекс.Диска')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print('Created synthetic files:', create(args.output))
