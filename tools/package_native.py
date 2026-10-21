"""Archive a native CLI build with executable permissions and user docs."""
import argparse
from pathlib import Path
import platform
import sys
import tarfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from disk_downloader import __version__


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--binary',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if sys.platform not in ('darwin','linux'):parser.error('Build on macOS or Linux')
    binary=args.binary.resolve(strict=True);root=Path(__file__).resolve().parents[1]
    system='macOS' if sys.platform=='darwin' else 'Linux';machine=platform.machine().lower();arch='arm64' if machine in ('arm64','aarch64') else 'x64' if machine in ('amd64','x86_64') else machine
    args.output.mkdir(parents=True,exist_ok=True)
    output=args.output/f'YaDiskDownloader-v{__version__}-{system}-{arch}.tar.gz'
    def executable(info):
        info.uid=info.gid=0;info.uname=info.gname=''
        info.mode=0o755 if Path(info.name).name=='yadisk' else 0o644
        return info
    files=[(binary,'yadisk'),(binary.parent/'THIRD_PARTY_NOTICES.txt','THIRD_PARTY_NOTICES.txt')]+[(root/name,name) for name in ('LICENSE','README.md','docs/cli.md','docs/features-v1.5.0.md')]
    with tarfile.open(output,'w:gz') as archive:
        for source,name in files:
            if not source.is_file() or source.is_symlink():raise ValueError('Missing regular distribution file')
            archive.add(source,arcname='YaDiskDownloader/'+name,recursive=False,filter=executable)
    print(output.name)


if __name__=='__main__':main()
