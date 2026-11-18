"""Read RAR headers only, inside the parent adapter's bounded child process."""
import json
import sys


def main():
    try:
        import rarfile
    except ImportError:
        print(json.dumps({'unsupported': 'rarfile metadata dependency is not installed'}))
        return
    with rarfile.RarFile(sys.argv[1]) as archive:
        infos = archive.infolist()
        if len(infos) > int(sys.argv[2]):
            raise ValueError('ARCHIVE_MEMBER_COUNT_LIMIT')
        result = {'encrypted': archive.needs_password(), 'multivolume': len(archive.volumelist()) > 1,
                  'metadata_engine': 'rarfile/' + rarfile.__version__, 'entries': []}
        for info in infos:
            result['entries'].append({'name': info.filename, 'size': info.file_size,
                'compressed': info.compress_size, 'crc32': getattr(info, 'CRC', None),
                'directory': info.is_dir(), 'link': info.is_symlink() or bool(getattr(info, 'file_redir', None)),
                'special': not (info.is_file() or info.is_dir()), 'mode': getattr(info, 'mode', None)})
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
