#!/usr/bin/env python3
"""
Delete duplicate files found by jdupes, keeping one copy of each file and
recording the paths of the deleted copies in a sidecar file next to it.
"""

import argparse
import collections
import contextlib
import logging
import os
import re
import shlex
import signal
import subprocess
import sys

# tqdm is optional: it is only used to draw the --progress bar.
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

log = logging.getLogger('jdupes-sidecar')

# jdupes' long options (1.27.3 to 1.31.2): the short option each one is the same as, and
# whether it takes a value. Needed to read arguments exactly as jdupes will, including
# abbreviations such as --chunk for --chunk-size.
JDUPES_LONG_OPTIONS = {
    'loud': ('@', False), 'print-null': ('0', False), 'one-file-system': ('1', False),
    'no-hidden': ('A', False), 'dedupe': ('B', False), 'hash-db-opts': ('b', True),
    'chunk-size': ('C', True), 'debug': ('D', False), 'delete': ('d', False),
    'error-on-dupe': ('e', False), 'ext-option': ('E', False), 'omit-first': ('f', False),
    'hard-links': ('H', False), 'help': ('h', False), 'isolate': ('I', False),
    'reverse': ('i', False), 'json': ('j', False), 'skip-hash': ('K', False),
    'link-hard': ('L', False), 'link-soft': ('l', False), 'print-summarize': ('M', False),
    'summarize': ('m', False), 'no-prompt': ('N', False), 'param-order': ('O', False),
    'order': ('o', True), 'print': ('P', True), 'permissions': ('p', False),
    'quick': ('Q', False), 'quiet': ('q', False), 'recurse:': ('R', False),
    'recurse': ('r', False), 'size': ('S', False), 'symlinks': ('s', False),
    'partial-only': ('T', False), 'no-change-check': ('t', False),
    'no-trav-check': ('U', False), 'print-unique': ('u', False), 'version': ('v', False),
    'ext-filter': ('X', True), 'hash-db': ('y', True), 'hash-db-populate': ('Y', True),
    'soft-abort': ('Z', False), 'zero-match': ('z', False),
}
JDUPES_SHORT_OPTIONS_WITH_VALUE = set('bCoPXy')
LONG_OPTION_NAMES = {letter: '--' + name for name, (letter, _) in JDUPES_LONG_OPTIONS.items()}
# The jdupes options that can be passed through --jdupes-extra-args: those that only
# affect which files are compared and how. All others are rejected, because they make
# jdupes act on the duplicates itself (bypassing the sidecar files), change the output
# this script parses, make matches unsafe to delete, or are unknown.
ALLOWED_OPTIONS = set('@01ACHiIoOpqQrstUXyzZ')

# Signals that are held back while a duplicate is deleted and recorded.
DEFERRED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class FatalError(Exception):
    """An error that stops the run before any file is touched."""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Deduplicate files using jdupes and record deleted duplicates in sidecar files. '
                    'Directories are listed in priority order: the copy in the earliest directory is kept.'
    )
    parser.add_argument('directories', nargs='+', help='Directories to deduplicate, highest priority first')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='Report what would be done without deleting or writing anything')
    parser.add_argument('-o', '--output', default='dry_run_output.txt',
                        help="File for the dry run report, or '-' for standard output (default: dry_run_output.txt)")
    parser.add_argument('--yes', action='store_true',
                        help='Do not ask for confirmation before deleting (for unattended runs)')
    parser.add_argument('-v', '--verbose', action='count', default=0,
                        help='Increase output verbosity (can be used multiple times)')
    parser.add_argument('--progress', action='store_true',
                        help='Show jdupes scan progress and a progress bar while processing (bar requires tqdm)')
    parser.add_argument('--jdupes-path', default='jdupes', help='Path to the jdupes binary (default: jdupes)')
    parser.add_argument('--jdupes-hashdb', help='Path to the hash database file to be used by jdupes')
    parser.add_argument('--jdupes-extra-args', default='',
                        help='Extra arguments to pass to jdupes, e.g. --jdupes-extra-args="--no-hidden --zero-match"')
    # Former name of --jdupes-extra-args.
    parser.add_argument('--jdupes-extra-cmds', dest='jdupes_extra_args', help=argparse.SUPPRESS)
    parser.add_argument('--sidecar-extension', default='.dupes',
                        help='File extension for sidecar files (default: .dupes)')
    parser.add_argument('--no-exclude-sidecar', action='store_true',
                        help='Do not exclude sidecar files from jdupes processing')
    parser.add_argument('--no-merge-existing-sidecars', action='store_true',
                        help='Do not merge existing sidecar files from deletion candidates')
    parser.add_argument('--no-delete-duplicate-sidecar', action='store_true',
                        help='Do not delete sidecar files of duplicate files after merging')
    args = parser.parse_args(argv)

    # jdupes' extension filter only looks at the last extension, so a sidecar extension
    # containing a dot (or a ',' list separator) would silently stop excluding sidecars.
    ext = args.sidecar_extension[1:] if args.sidecar_extension.startswith('.') else args.sidecar_extension
    if not re.fullmatch(r'[\w-]+', ext):
        parser.error(f"invalid --sidecar-extension {args.sidecar_extension!r}: "
                     "use a single extension of letters, digits, '_' or '-', such as .dupes")
    args.sidecar_extension = '.' + ext

    try:
        args.jdupes_extra_args = shlex.split(args.jdupes_extra_args or '')
    except ValueError as e:
        parser.error(f"cannot parse --jdupes-extra-args: {e}")
    problem = check_extra_args(args.jdupes_extra_args)
    if problem:
        parser.error(f"--jdupes-extra-args: {problem}")

    return args


def split_options(extra_args):
    """
    Work out which jdupes options a list of arguments selects, reading them as jdupes does.

    Args:
        extra_args (list): jdupes arguments, already split into words.

    Returns:
        list: The short option letter of each option given (a group like -rQ gives 'r'
        and 'Q'; --quick and its abbreviations give 'Q').

    Raises:
        ValueError: If an argument is not a jdupes option, or an option's value is missing.
    """
    options = []
    args = iter(extra_args)
    for arg in args:
        if not arg.startswith('-') or arg in ('-', '--'):
            raise ValueError(f"{arg!r} is not an option; pass directories as positional arguments instead")
        if arg.startswith('--'):
            name, has_value, _ = arg[2:].partition('=')
            # Like jdupes (getopt_long), take an exact name or else an unambiguous abbreviation.
            matches = [name] if name in JDUPES_LONG_OPTIONS else [
                option for option in JDUPES_LONG_OPTIONS if option.startswith(name)]
            if len(matches) != 1:
                raise ValueError(f"{arg} is {'an ambiguous' if matches else 'not a'} jdupes option")
            letter, takes_value = JDUPES_LONG_OPTIONS[matches[0]]
            if has_value and not takes_value:
                raise ValueError(f"{arg}: --{matches[0]} does not take a value")
            if takes_value and not has_value and next(args, None) is None:
                raise ValueError(f"{arg} needs a value")
            options.append(letter)
            continue
        for position, letter in enumerate(arg[1:], start=1):
            options.append(letter)
            if letter in JDUPES_SHORT_OPTIONS_WITH_VALUE:
                # The rest of the word is the value, or else the next word is.
                if position == len(arg) - 1 and next(args, None) is None:
                    raise ValueError(f"-{letter} needs a value")
                break
    return options


def check_extra_args(extra_args):
    """Return why arguments cannot be passed through to jdupes, or None if they can."""
    try:
        options = split_options(extra_args)
    except ValueError as e:
        return str(e)
    for letter in options:
        if letter not in ALLOWED_OPTIONS:
            name = f" ({LONG_OPTION_NAMES[letter]})" if letter in LONG_OPTION_NAMES else ""
            return f"-{letter}{name} is not supported because this script must control jdupes' actions and output"
    return None


def build_jdupes_command(args):
    cmd = [args.jdupes_path, '--param-order', '--recurse', '--print-null']
    if not args.progress:
        cmd.append('--quiet')
    if args.jdupes_hashdb:
        cmd.append(f'--hash-db={args.jdupes_hashdb}')
    if not args.no_exclude_sidecar:
        cmd.append(f'--ext-filter=noext:{args.sidecar_extension[1:]}')
    cmd += args.jdupes_extra_args
    cmd.append('--')
    cmd += args.directories
    return cmd


def run_jdupes(cmd):
    """Run jdupes and return its match sets. jdupes' own messages go straight to stderr."""
    log.debug(f"Running command: {shlex.join(cmd)}")
    try:
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    except OSError as e:
        raise FatalError(f"Cannot run jdupes ({cmd[0]}): {e.strerror}") from None
    if result.returncode < 0:
        raise FatalError(f"jdupes was killed by signal {signal.Signals(-result.returncode).name}")
    if result.returncode != 0:
        raise FatalError(f"jdupes exited with status {result.returncode}")
    return parse_jdupes_output(result.stdout)


def parse_jdupes_output(output):
    """
    Parse the output of `jdupes --print-null`.

    Every path is NUL-terminated and match sets are separated by an empty entry. Paths
    are handled as raw bytes, so any file name works, including ones that are not valid
    UTF-8 (jdupes' JSON output cannot represent those and hangs on some of them).

    Args:
        output (bytes): jdupes' standard output.

    Returns:
        list: Match sets, each a list of paths in the order jdupes printed them.
    """
    *entries, trailer = output.split(b'\0')
    # Text after the last NUL is not a path. jdupes prints "No duplicates found." there
    # when nothing matched; anything else (such as help text) means we misread the output.
    if trailer.strip() not in ((b'',) if entries else (b'', b'No duplicates found.')):
        raise FatalError(f"Unexpected output from jdupes: {trailer[:200]!r}")
    match_sets, current = [], []
    for entry in entries:
        if entry:
            current.append(os.fsdecode(entry))
        elif current:
            match_sets.append(current)
            current = []
    if current:
        match_sets.append(current)
    return match_sets


def canonical_path(path):
    """
    Return an absolute path to the same directory entry, with symlinked directories resolved.

    Unlike os.path.abspath, this resolves symlinks before '..', as the kernel does:
    'link/../f' is not necessarily './f'. The last component is kept as it is, so a
    path to a symlink still names the symlink.
    """
    directory, name = os.path.split(os.path.join(os.getcwd(), path))
    return os.path.join(os.path.realpath(directory), name)


def order_by_priority(paths, directories):
    """
    Order paths by the position of the directory they are in.

    Args:
        paths (list): Paths of identical files, in jdupes' order.
        directories (list): Directory paths in priority order.

    Returns:
        list: Canonical paths (see canonical_path), each listed once. Paths in the same
        directory keep their relative order; paths outside every directory come last.
    """
    # A path belongs to the first directory that contains it, so overlapping directories
    # (or one directory given twice) never list a file twice. Listing the kept file
    # again as a duplicate would delete every copy.
    roots = [os.path.realpath(d) for d in directories]
    ranked = {}
    for path in paths:
        path = canonical_path(path)
        if path not in ranked:
            ranked[path] = next((rank for rank, root in enumerate(roots)
                                 if path == root or path.startswith(os.path.join(root, ''))), len(roots))
    # sorted() is stable, so ties keep jdupes' order.
    return sorted(ranked, key=ranked.get)


# What must stay the same for a file to count as unchanged.
FileState = collections.namedtuple('FileState', 'dev ino size mtime_ns')


def file_state(stat_result):
    return FileState(stat_result.st_dev, stat_result.st_ino, stat_result.st_size, stat_result.st_mtime_ns)


def snapshot(paths):
    """Record the state of files, to notice later if they changed. Missing files map to None."""
    states = {}
    for path in paths:
        try:
            states[path] = file_state(os.stat(path))
        except OSError:
            states[path] = None
    return states


def read_sidecar(path):
    """
    Read a sidecar file.

    Returns:
        bytes: Its contents, or None if it does not look like a sidecar file (one absolute
        path on each line).
    """
    with open(path, 'rb') as f:
        data = f.read()
    lines = data.split(b'\n')
    if lines.pop() != b'' or not all(line.startswith(b'/') for line in lines):
        return None
    return data


@contextlib.contextmanager
def signals_deferred():
    """Hold back Ctrl-C and termination signals until the block has finished."""
    received = []
    previous = {signum: signal.signal(signum, lambda number, frame: received.append(number))
                for signum in DEFERRED_SIGNALS}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if received:
            signal.raise_signal(received[0])


def process_set(keep, dupes, args, report, scan):
    """
    Delete the duplicates of one file and record them in its sidecar file.

    In dry run mode, only describe what would be done, in `report`.

    Args:
        keep (str): The file to keep.
        dupes (list): Its duplicates, to delete.
        args: The parsed command line.
        report (list): Lines of the dry run report, to add to.
        scan (dict): The state of every file in every match set right after the scan,
            from snapshot().

    Returns:
        tuple: (number of duplicates deleted, number of problems encountered).
    """
    ext = args.sidecar_extension
    merge = not args.no_merge_existing_sidecars
    sidecar = keep + ext
    problems = 0

    def skip(path, reason):
        nonlocal problems
        problems += 1
        log.warning(f"Not deleting {path}: {reason}")
        if args.dry_run:
            report.append(f"Would skip duplicate file: {path} ({reason})")

    def skip_set(reason):
        log.error(f"Skipping the duplicates of {keep}: {reason}")
        if args.dry_run:
            report.extend([f"Would skip the duplicates of {keep}: {reason}", ""])
        return 0, problems + 1

    try:
        keep_stat = os.stat(keep)
    except OSError as e:
        return skip_set(f"cannot access it: {e.strerror}")
    if file_state(keep_stat) != scan.get(keep):
        return skip_set("it changed after the scan")
    # Only possible with --no-exclude-sidecar: the sidecar file would overwrite or be
    # mixed up with a file that is being deduplicated itself.
    if sidecar in scan:
        return skip_set(f"its sidecar file {sidecar} is one of the duplicates found")
    if args.dry_run:
        report.append(f"Would keep file: {keep}")

    # Decide which duplicates to delete, and what each one adds to the sidecar file,
    # before touching anything.
    entries = []
    for dupe in dupes:
        try:
            dupe_stat = os.stat(dupe)
        except OSError as e:
            skip(dupe, f"cannot access it: {e.strerror}")
            continue
        if os.path.samestat(dupe_stat, keep_stat):
            # A hard link to the kept file (only matched with -H), or the kept file reached
            # through a symlink. Deleting it would reclaim nothing, or delete the kept file.
            log.info(f"Not deleting {dupe}: it is the same file as {keep}")
            if args.dry_run:
                report.append(f"Would not delete {dupe}: it is the same file as the kept file")
            continue
        if '\n' in dupe:
            skip(dupe, "its path contains a newline, which cannot be recorded in a sidecar file")
            continue
        if dupe_stat.st_size != keep_stat.st_size:
            skip(dupe, "its size differs from the kept file's, so one of them changed during the scan")
            continue
        if file_state(dupe_stat) != scan.get(dupe):
            skip(dupe, "it changed after the scan")
            continue
        record = os.fsencode(dupe) + b'\n'
        dupe_sidecar = dupe + ext
        if merge and os.path.lexists(dupe_sidecar):
            if dupe_sidecar in scan:
                skip(dupe, f"its sidecar file {dupe_sidecar} is one of the duplicates found")
                continue
            try:
                contents = read_sidecar(dupe_sidecar)
            except OSError as e:
                skip(dupe, f"cannot read its sidecar file {dupe_sidecar}: {e.strerror}")
                continue
            if contents is None:
                skip(dupe, f"{dupe_sidecar} does not look like a sidecar file, so it cannot be merged")
                continue
            record += contents
        else:
            dupe_sidecar = None
        entries.append((dupe, dupe_sidecar, record))

    if not entries:
        if args.dry_run:
            report.append("")
        return 0, problems

    if args.dry_run:
        for dupe, dupe_sidecar, _ in entries:
            report.append(f"Would delete duplicate file: {dupe}")
            if dupe_sidecar:
                report.append(f"Would merge existing sidecar file: {dupe_sidecar} into {sidecar}")
                verb = 'not delete' if args.no_delete_duplicate_sidecar else 'delete'
                report.append(f"Would {verb} sidecar file: {dupe_sidecar}")
        verb = 'append to' if os.path.lexists(sidecar) else 'create'
        report.append(f"Would {verb} sidecar file: {sidecar} with contents:")
        report.extend(f"  {os.fsdecode(line)}" for _, _, record in entries for line in record[:-1].split(b'\n'))
        report.append("")
        return len(entries), problems

    # Open the sidecar file before deleting anything, so that a duplicate is only
    # deleted when its path can be recorded.
    existed = os.path.lexists(sidecar)
    try:
        sidecar_file = open(sidecar, 'ab')
    except OSError as e:
        return skip_set(f"cannot open sidecar file {sidecar}: {e.strerror}")

    deleted, merged_sidecars = [], []
    try:
        with sidecar_file:
            for dupe, dupe_sidecar, record in entries:
                # Ctrl-C between deleting a file and recording it would lose the record.
                with signals_deferred():
                    try:
                        os.remove(dupe)
                    except OSError as e:
                        log.error(f"Error deleting {dupe}: {e.strerror}")
                        problems += 1
                        continue
                    deleted.append(dupe)
                    sidecar_file.write(record)
                    sidecar_file.flush()
                log.info(f"Deleted duplicate file: {dupe}")
                if dupe_sidecar:
                    merged_sidecars.append(dupe_sidecar)
    except OSError as e:
        log.error(f"Error writing sidecar file {sidecar}: {e.strerror}. "
                  f"It may be missing these deleted files: {', '.join(deleted)}")
        return len(deleted), problems + 1

    if not deleted:
        if not existed:
            try:
                os.remove(sidecar)
            except OSError:
                pass
        return 0, problems
    log.info(f"{'Appended to' if existed else 'Created'} sidecar file: {sidecar}")

    # The merged sidecars' contents are now safely in the kept file's sidecar.
    for merged in merged_sidecars:
        if args.no_delete_duplicate_sidecar:
            log.info(f"Retained sidecar file: {merged}")
            continue
        try:
            os.remove(merged)
            log.info(f"Deleted merged sidecar file: {merged}")
        except OSError as e:
            log.error(f"Error deleting merged sidecar file {merged}: {e.strerror}")
            problems += 1
    return len(deleted), problems


def format_size(size):
    if size < 1024:
        return f"{size} bytes"
    for unit in ('KiB', 'MiB', 'GiB', 'TiB'):
        size /= 1024
        if size < 1024 or unit == 'TiB':
            return f"{size:.1f} {unit}"


def plural(count, noun):
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def confirm(prompt):
    """Ask a yes/no question. Returns None if there is no one to answer it."""
    try:
        return input(prompt).strip().lower() in ('y', 'yes')
    except EOFError:
        print()
        return None


def write_report(report, output):
    # os.fsencode() turns paths back into the exact bytes jdupes printed.
    data = os.fsencode('\n'.join(report) + '\n')
    if output == '-':
        sys.stdout.flush()
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
        return
    try:
        with open(output, 'wb') as f:
            f.write(data)
    except OSError as e:
        raise FatalError(f"Error writing dry run report to {output}: {e.strerror}") from None
    print(f"Dry run report written to {output}")


def run(args):
    for directory in args.directories:
        # jdupes skips file arguments with only a warning, so insist on directories.
        if not os.path.isdir(directory):
            raise FatalError(f"Not a directory: {directory}")

    if args.dry_run:
        print("Dry run mode: No files will be deleted or modified.")
    if 'Q' in split_options(args.jdupes_extra_args):
        log.warning("Warning: --quick skips jdupes' byte-for-byte comparison; "
                    "a hash collision could delete a file that is not a duplicate.")

    match_sets = run_jdupes(build_jdupes_command(args))
    plans = []
    for paths in match_sets:
        ordered = order_by_priority(paths, args.directories)
        if len(ordered) > 1:
            plans.append((ordered[0], ordered[1:]))
    scan = snapshot(path for keep, dupes in plans for path in [keep] + dupes)

    if not plans:
        print("No duplicates found.")
        if args.dry_run:
            write_report([], args.output)
        return 0

    total_dupes, total_bytes = 0, 0
    for keep, dupes in plans:
        total_dupes += len(dupes)
        if scan[keep]:
            total_bytes += scan[keep].size * len(dupes)
    print(f"Found {plural(len(plans), 'set')} of duplicates: {plural(total_dupes, 'file')} to delete, "
          f"reclaiming up to {format_size(total_bytes)}.")

    if not args.dry_run and not args.yes:
        answer = confirm("Delete these duplicates and write sidecar files? [y/N]: ")
        if answer is None:
            log.error("No confirmation received because standard input is closed. "
                      "Use --yes to run without confirmation.")
            return 1
        if not answer:
            print("Operation cancelled by user.")
            return 0

    if args.progress and tqdm is None:
        log.warning("Install the 'tqdm' module to show a progress bar.")
    progress = tqdm(total=len(plans), desc='Processing duplicates') if args.progress and tqdm else None

    report, deleted, problems = [], 0, 0
    for keep, dupes in plans:
        set_deleted, set_problems = process_set(keep, dupes, args, report, scan)
        deleted += set_deleted
        problems += set_problems
        if progress:
            progress.update(1)
    if progress:
        progress.close()

    if args.dry_run:
        write_report(report, args.output)
        print(f"Would delete {plural(deleted, 'duplicate file')}.")
    else:
        print(f"Deleted {plural(deleted, 'duplicate file')}.")
    if problems:
        log.error(f"{plural(problems, 'problem')} occurred; see the messages above.")
        return 1
    return 0


def main(argv=None):
    args = parse_args(argv)
    if args.verbose == 0:
        log_level = logging.WARNING
    elif args.verbose == 1:
        log_level = logging.INFO
    else:
        log_level = logging.DEBUG
    logging.basicConfig(format='%(message)s', level=log_level)

    try:
        return run(args)
    except FatalError as e:
        log.error(f"Error: {e}")
        return 1
    except KeyboardInterrupt:
        log.error("Interrupted.")
        return 130


if __name__ == '__main__':
    sys.exit(main())
