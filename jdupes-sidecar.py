#!/usr/bin/env python3
"""
Delete duplicate files found by jdupes, keeping one copy of each file and
recording the paths of the deleted copies in a sidecar file next to it.
"""

import argparse
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

# jdupes options that must not be passed through --jdupes-extra-args. They either make
# jdupes act on the duplicates itself (bypassing the sidecar files), change the output
# this script parses, or make matches unsafe to delete.
FORBIDDEN_SHORT_OPTIONS = {
    'B': '--dedupe',
    'd': '--delete',
    'e': '--error-on-dupe',
    'f': '--omit-first',
    'h': '--help',
    'j': '--json',
    'l': '--link-soft',
    'L': '--link-hard',
    'm': '--summarize',
    'M': '--print-summarize',
    'N': '--no-prompt',
    'P': '--print',
    'S': '--size',
    'T': '--partial-only',
    'u': '--print-unique',
    'v': '--version',
    'Y': '--hash-db-populate',
}
FORBIDDEN_LONG_OPTIONS = set(FORBIDDEN_SHORT_OPTIONS.values())
# jdupes options that take a value, as `-X value`, `-Xvalue`, `--ext-filter value`
# or `--ext-filter=value`.
SHORT_OPTIONS_WITH_VALUE = set('CoPXy')
LONG_OPTIONS_WITH_VALUE = {'--chunk-size', '--order', '--print', '--ext-filter', '--hash-db'}


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
                        help='Extra arguments to pass to jdupes, e.g. "--no-hidden --zero-match"')
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
    List the options in arguments meant for jdupes, without their values.

    Args:
        extra_args (list): jdupes arguments, already split into words.

    Returns:
        list: A letter for each short option (a group like -rQ gives 'r' and 'Q') and
        the name of each long option (such as '--quick').

    Raises:
        ValueError: If an argument is not an option.
    """
    options = []
    args = iter(extra_args)
    for arg in args:
        if not arg.startswith('-') or arg in ('-', '--'):
            raise ValueError(f"{arg!r} is not an option; pass directories as positional arguments instead")
        if arg.startswith('--'):
            name, has_value, _ = arg.partition('=')
            options.append(name)
            if name in LONG_OPTIONS_WITH_VALUE and not has_value:
                next(args, None)
            continue
        for position, letter in enumerate(arg[1:], start=1):
            options.append(letter)
            if letter in SHORT_OPTIONS_WITH_VALUE:
                if position == len(arg) - 1:
                    next(args, None)
                break
    return options


def check_extra_args(extra_args):
    """Return why arguments cannot be passed through to jdupes, or None if they can."""
    try:
        options = split_options(extra_args)
    except ValueError as e:
        return str(e)
    for option in options:
        if option in FORBIDDEN_SHORT_OPTIONS:
            option = f"-{option} ({FORBIDDEN_SHORT_OPTIONS[option]})"
        elif option in FORBIDDEN_LONG_OPTIONS:
            pass
        # jdupes also accepts unambiguous abbreviations of long options (--del for --delete).
        elif option in LONG_OPTIONS_WITH_VALUE or not any(
                forbidden.startswith(option) for forbidden in FORBIDDEN_LONG_OPTIONS):
            continue
        return f"{option} is not supported because this script must control jdupes' actions and output"
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
    # when nothing matched; with matches, anything but whitespace means we misread the output.
    if entries and trailer.strip():
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


def order_by_priority(paths, directories):
    """
    Order paths by the position of the directory they are in.

    Args:
        paths (list): Paths of identical files, in jdupes' order.
        directories (list): Directory paths in priority order.

    Returns:
        list: Absolute paths, each listed once. Paths in the same directory keep their
        relative order; paths outside every directory come last.
    """
    # A path belongs to the first directory that contains it, so overlapping directories
    # (or one directory given twice) never list a file twice. Listing the kept file
    # again as a duplicate would delete every copy.
    roots = [os.path.abspath(d) for d in directories]
    ranked = {}
    for path in paths:
        path = os.path.abspath(path)
        if path not in ranked:
            ranked[path] = next((rank for rank, root in enumerate(roots)
                                 if path == root or path.startswith(os.path.join(root, ''))), len(roots))
    # sorted() is stable, so ties keep jdupes' order.
    return sorted(ranked, key=ranked.get)


def read_sidecar(path):
    """Return the entries of a sidecar file as a list of bytes."""
    with open(path, 'rb') as f:
        return [line for line in f.read().split(b'\n') if line]


def deletion_problem(dupe, dupe_stat, keep_stat):
    """Return why a duplicate must not be deleted, or None if it can be."""
    if '\n' in dupe:
        return "its path contains a newline, which cannot be recorded in a sidecar file"
    if dupe_stat.st_size != keep_stat.st_size:
        return "its size differs from the kept file's, so a file changed after jdupes scanned it"
    return None


def process_set(keep, dupes, args, report):
    """
    Delete the duplicates of one file and record them in its sidecar file.

    In dry run mode, only describe what would be done, in `report`.

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

    try:
        keep_stat = os.stat(keep)
    except OSError as e:
        log.error(f"Skipping the duplicates of {keep}: cannot access it: {e.strerror}")
        if args.dry_run:
            report.extend([f"Would skip the duplicates of {keep}: cannot access it: {e.strerror}", ""])
        return 0, 1
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
        problem = deletion_problem(dupe, dupe_stat, keep_stat)
        if problem:
            skip(dupe, problem)
            continue
        lines = [os.fsencode(dupe)]
        dupe_sidecar = dupe + ext
        if merge and os.path.lexists(dupe_sidecar):
            try:
                lines += read_sidecar(dupe_sidecar)
            except OSError as e:
                skip(dupe, f"cannot read its sidecar file {dupe_sidecar}: {e.strerror}")
                continue
        else:
            dupe_sidecar = None
        entries.append((dupe, dupe_sidecar, lines))

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
        report.extend(f"  {os.fsdecode(line)}" for _, _, lines in entries for line in lines)
        report.append("")
        return len(entries), problems

    # Open the sidecar file before deleting anything, so that a duplicate is only
    # deleted when its path can be recorded.
    existed = os.path.lexists(sidecar)
    try:
        sidecar_file = open(sidecar, 'ab')
    except OSError as e:
        log.error(f"Skipping the duplicates of {keep}: cannot open sidecar file {sidecar}: {e.strerror}")
        return 0, problems + 1

    deleted, merged_sidecars = [], []
    try:
        with sidecar_file:
            for dupe, dupe_sidecar, lines in entries:
                try:
                    os.remove(dupe)
                except OSError as e:
                    log.error(f"Error deleting {dupe}: {e.strerror}")
                    problems += 1
                    continue
                deleted.append(dupe)
                log.info(f"Deleted duplicate file: {dupe}")
                sidecar_file.write(b''.join(line + b'\n' for line in lines))
                sidecar_file.flush()
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
        if not os.path.exists(directory):
            raise FatalError(f"No such file or directory: {directory}")

    if args.dry_run:
        print("Dry run mode: No files will be deleted or modified.")
    if any(option in ('Q', '--quick') for option in split_options(args.jdupes_extra_args)):
        log.warning("Warning: --quick skips jdupes' byte-for-byte comparison; "
                    "a hash collision could delete a file that is not a duplicate.")

    match_sets = run_jdupes(build_jdupes_command(args))
    plans = []
    for paths in match_sets:
        ordered = order_by_priority(paths, args.directories)
        if len(ordered) > 1:
            plans.append((ordered[0], ordered[1:]))

    if not plans:
        print("No duplicates found.")
        if args.dry_run:
            write_report([], args.output)
        return 0

    total_dupes, total_bytes = 0, 0
    for keep, dupes in plans:
        total_dupes += len(dupes)
        try:
            total_bytes += os.stat(keep).st_size * len(dupes)
        except OSError:
            pass
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
        set_deleted, set_problems = process_set(keep, dupes, args, report)
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
