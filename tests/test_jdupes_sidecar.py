"""
Tests for jdupes-sidecar.py.

Run with `python3 -m unittest discover tests`. The end-to-end tests need jdupes; they
use the `jdupes` on PATH, or the binary named by the JDUPES environment variable.
"""

import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'jdupes-sidecar.py')
JDUPES = os.environ.get('JDUPES') or shutil.which('jdupes')

# The script's name is not a valid module name, so load it by path.
_spec = importlib.util.spec_from_file_location('jdupes_sidecar', SCRIPT)
jdupes_sidecar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jdupes_sidecar)


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        # realpath, because the script records paths based on os.getcwd(), which is resolved.
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def path(self, name):
        if isinstance(name, bytes):
            return os.path.join(os.fsencode(self.tmp), name)
        return os.path.join(self.tmp, name)

    def write(self, name, content='same content\n'):
        path = self.path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(content.encode() if isinstance(content, str) else content)
        return path

    def read(self, name):
        with open(self.path(name), 'rb') as f:
            return f.read()

    def snapshot(self):
        """Every file under the temp directory, with its contents."""
        files = {}
        for root, _, names in os.walk(os.fsencode(self.tmp)):
            for name in names:
                path = os.path.join(root, name)
                with open(path, 'rb') as f:
                    files[path] = f.read()
        return files


class ParseJdupesOutputTest(unittest.TestCase):
    parse = staticmethod(jdupes_sidecar.parse_jdupes_output)

    def test_sets_separated_by_empty_entries(self):
        # jdupes 1.27.3 ends with an extra separator, 1.31.2 does not.
        self.assertEqual(self.parse(b'a\0b\0\0c\0d\0\0'), [['a', 'b'], ['c', 'd']])
        self.assertEqual(self.parse(b'a\0b\0\0c\0d\0'), [['a', 'b'], ['c', 'd']])

    def test_trailing_newline_is_ignored(self):
        # Unreleased jdupes versions print a newline after the last entry.
        self.assertEqual(self.parse(b'a\0b\0\n'), [['a', 'b']])

    def test_no_duplicates(self):
        self.assertEqual(self.parse(b'No duplicates found.\n'), [])
        self.assertEqual(self.parse(b''), [])

    def test_other_text_is_an_error(self):
        # Such as the help text printed for `-X help`.
        with self.assertRaises(jdupes_sidecar.FatalError):
            self.parse(b'Extension filters:\n  size+:SIZE ...\n')

    def test_unexpected_text_after_entries_is_an_error(self):
        with self.assertRaises(jdupes_sidecar.FatalError):
            self.parse(b'a\0b\0\n1 duplicate files (in 1 sets), occupying 5 bytes\n')

    def test_names_are_kept_byte_exact(self):
        [[bad, newline]] = self.parse(b'bad\xffname\0new\nline\0')
        self.assertEqual(os.fsencode(bad), b'bad\xffname')
        self.assertEqual(newline, 'new\nline')


class OrderByPriorityTest(unittest.TestCase):
    def order(self, paths, directories):
        return [os.path.relpath(p) for p in jdupes_sidecar.order_by_priority(paths, directories)]

    def test_directory_order_decides(self):
        self.assertEqual(self.order(['zz/f', 'aa/f', 'mm/f'], ['mm', 'zz', 'aa']), ['mm/f', 'zz/f', 'aa/f'])

    def test_order_within_a_directory_is_kept(self):
        self.assertEqual(self.order(['d/b', 'd/a'], ['d']), ['d/b', 'd/a'])

    def test_overlapping_directories_list_each_file_once(self):
        self.assertEqual(self.order(['a/b/x', 'a/b/z'], ['a', 'a/b']), ['a/b/x', 'a/b/z'])
        self.assertEqual(self.order(['a/y', 'a/b/x'], ['a/b', 'a']), ['a/b/x', 'a/y'])

    def test_same_directory_twice_lists_each_file_once(self):
        self.assertEqual(self.order(['d/p1', 'd/p2'], ['d', 'd/', './d']), ['d/p1', 'd/p2'])

    def test_repeated_paths_are_listed_once(self):
        self.assertEqual(self.order(['d/p1', 'd//p1', 'd/p2'], ['d']), ['d/p1', 'd/p2'])

    def test_directory_name_prefix_is_not_a_match(self):
        self.assertEqual(self.order(['ab/x', 'a/x'], ['a', 'ab']), ['a/x', 'ab/x'])

    def test_root_directory(self):
        self.assertEqual(jdupes_sidecar.order_by_priority(['/y/f', '/x/f'], ['/x', '/']), ['/x/f', '/y/f'])

    def test_files_given_directly(self):
        self.assertEqual(self.order(['d/f', 'g'], ['g', 'd']), ['g', 'd/f'])

    def test_paths_outside_all_directories_come_last(self):
        self.assertEqual(self.order(['other/f', 'd/f'], ['d']), ['d/f', 'other/f'])


class CheckExtraArgsTest(unittest.TestCase):
    check = staticmethod(jdupes_sidecar.check_extra_args)

    def test_allowed(self):
        for args in (['-A', '-z'], ['-X', 'noext:txt'], ['-Xnoext:dmfe'], ['--ext-filter', 'size+:1k'],
                     ['--ext-filter=noext:mp3'], ['--order', 'time'], ['-io', 'time'], ['-rQ'],
                     ['--quick'], ['--hash-db', 'x.db'], ['--hash-db=x.db'], ['-1', '-s', '-H', '-p'],
                     ['--no-hidden'], ['--no-h'], ['--zero'], ['--print-null'], ['-C', '16'], ['-tUZ@']):
            with self.subTest(args=args):
                self.assertIsNone(self.check(args))

    def test_forbidden(self):
        for args in (['-d'], ['-rdN'], ['-L'], ['-l'], ['-B'], ['-m'], ['-M'], ['-u'], ['-e'], ['-f'],
                     ['-S'], ['-P', 'early'], ['-j'], ['-T'], ['-Y'], ['-v'], ['-h'], ['-R'], ['-b', 'x'],
                     ['-D'], ['-E'], ['-K'], ['-V'], ['-9'], ['-x'], ['--delete'], ['--del'], ['--json'],
                     ['--print=early'], ['--size'], ['--omit-first'], ['--hash-db-populate=x'],
                     ['--partial-only'], ['--recurse:'], ['-A', '--summarize']):
            with self.subTest(args=args):
                self.assertIn('not supported', self.check(args))

    def test_invalid(self):
        for args in (['--li'], ['--recur'], ['--no'], ['--nonexistent'], ['--zero-match=1'],
                     ['-C'], ['-X'], ['-rX'], ['--order'], ['--chunk']):
            with self.subTest(args=args):
                self.assertIsNotNone(self.check(args))

    def test_values_are_read_as_jdupes_reads_them(self):
        # In each of these, jdupes takes '-o' as the value of the option before it, so
        # '-dN' is an option (--delete --no-prompt), not the value of -o.
        for args in (['--chunk', '-o', '-dN'], ['--chunk-s', '-o', '-dN'], ['-C', '-o', '-dN'],
                     ['-b', '-o', '-dN'], ['--hash-db-opts', '-o', '-dN'], ['--hash', '-o', '-dN']):
            with self.subTest(args=args):
                self.assertIsNotNone(self.check(args))
        self.assertIn('-d (--delete)', self.check(['--chunk', '-o', '-dN']))

    def test_positional_arguments_are_rejected(self):
        for args in (['somedir'], ['-A', 'somedir'], ['--'], ['-']):
            with self.subTest(args=args):
                self.assertIn('not an option', self.check(args))

    def test_split_options(self):
        args = ['-rQ', '-X', 'noext:d', '-Cd', '--order=time', '--hash-db', 'f', '--quic', '--chunk', '-o']
        self.assertEqual(jdupes_sidecar.split_options(args), ['r', 'Q', 'X', 'C', 'o', 'y', 'Q', 'C'])


class ParseArgsTest(unittest.TestCase):
    def test_sidecar_extension(self):
        for given, expected in (('.dupes', '.dupes'), ('dupes', '.dupes'), ('dup_es-2', '.dup_es-2')):
            with self.subTest(given=given):
                args = jdupes_sidecar.parse_args(['--sidecar-extension', given, 'd'])
                self.assertEqual(args.sidecar_extension, expected)

    def test_invalid_sidecar_extension(self):
        # jdupes only filters on the last extension, so .dupes.txt would not be excluded.
        for given in ('', '.', '.dupes.txt', 'a,b', 'a/b', '..dupes'):
            with self.subTest(given=given), self.assertRaises(SystemExit):
                with open(os.devnull, 'w') as devnull:
                    sys.stderr, stderr = devnull, sys.stderr
                    try:
                        jdupes_sidecar.parse_args([f'--sidecar-extension={given}', 'd'])
                    finally:
                        sys.stderr = stderr

    def test_old_extra_args_option_name(self):
        args = jdupes_sidecar.parse_args(['--jdupes-extra-cmds', '-A -z', 'd'])
        self.assertEqual(args.jdupes_extra_args, ['-A', '-z'])


class FormatSizeTest(unittest.TestCase):
    def test_format_size(self):
        self.assertEqual(jdupes_sidecar.format_size(0), '0 bytes')
        self.assertEqual(jdupes_sidecar.format_size(1023), '1023 bytes')
        self.assertEqual(jdupes_sidecar.format_size(1536), '1.5 KiB')
        self.assertEqual(jdupes_sidecar.format_size(3 * 1024 ** 3), '3.0 GiB')
        self.assertEqual(jdupes_sidecar.format_size(5 * 1024 ** 4), '5.0 TiB')


class ProcessSetTest(TempDirTestCase):
    """Situations that cannot be set up through jdupes, such as files changing after the scan."""

    def args(self, **overrides):
        args = jdupes_sidecar.parse_args(['unused'])
        args.__dict__.update(overrides)
        return args

    def process(self, keep, dupes, scan=None, **overrides):
        if scan is None:
            scan = jdupes_sidecar.snapshot([keep] + dupes)
        with self.assertLogs(jdupes_sidecar.log, 'DEBUG') as logs:
            jdupes_sidecar.log.debug('start')
            result = jdupes_sidecar.process_set(keep, dupes, self.args(**overrides), [], scan)
        return result, '\n'.join(logs.output)

    def change(self, path, content):
        """Rewrite a file, making sure its modification time changes."""
        mtime = os.stat(path).st_mtime_ns
        with open(path, 'w') as f:
            f.write(content)
        os.utime(path, ns=(mtime + 10 ** 9, mtime + 10 ** 9))

    def test_duplicate_that_changed_size_is_not_deleted(self):
        keep, dupe = self.write('a/f'), self.write('b/f', 'changed during the scan\n')
        result, logs = self.process(keep, [dupe])
        self.assertEqual(result, (0, 1))
        self.assertTrue(os.path.exists(dupe))
        self.assertIn('size differs', logs)
        self.assertFalse(os.path.exists(keep + '.dupes'))

    def test_duplicate_that_changed_after_the_scan_is_not_deleted(self):
        keep, dupe = self.write('a/f', 'draft-1\n'), self.write('b/f', 'draft-1\n')
        scan = jdupes_sidecar.snapshot([keep, dupe])
        self.change(dupe, 'final-2\n')
        result, logs = self.process(keep, [dupe], scan)
        self.assertEqual(result, (0, 1))
        self.assertEqual(self.read('b/f'), b'final-2\n')
        self.assertIn('changed after the scan', logs)

    def test_nothing_is_deleted_if_kept_file_changed_after_the_scan(self):
        keep, dupe = self.write('a/f', 'draft-1\n'), self.write('b/f', 'draft-1\n')
        scan = jdupes_sidecar.snapshot([keep, dupe])
        self.change(keep, 'final-2\n')
        result, logs = self.process(keep, [dupe], scan)
        self.assertEqual(result, (0, 1))
        self.assertTrue(os.path.exists(dupe))

    def test_nothing_is_deleted_if_kept_file_is_gone(self):
        dupe = self.write('b/f')
        result, logs = self.process(self.path('a/f'), [dupe])
        self.assertEqual(result, (0, 1))
        self.assertTrue(os.path.exists(dupe))

    def test_missing_duplicate_is_reported(self):
        keep, dupe = self.write('a/f'), self.write('b/f')
        result, _ = self.process(keep, [self.path('b/gone'), dupe])
        self.assertEqual(result, (1, 1))
        self.assertEqual(self.read('a/f.dupes'), dupe.encode() + b'\n')

    def test_hard_link_to_kept_file_is_not_deleted(self):
        keep = self.write('a/f')
        os.link(keep, self.path('a/g'))
        result, logs = self.process(keep, [self.path('a/g')])
        self.assertEqual(result, (0, 0))
        self.assertTrue(os.path.exists(self.path('a/g')))
        self.assertIn('same file', logs)
        self.assertFalse(os.path.exists(keep + '.dupes'))

    def test_existing_sidecar_is_merged_verbatim(self):
        keep, dupe = self.write('a/f'), self.write('b/f')
        self.write('b/f.dupes', b'/old/caf\xe9\n/old/two\n')
        result, _ = self.process(keep, [dupe])
        self.assertEqual(result, (1, 0))
        self.assertEqual(self.read('a/f.dupes'), dupe.encode() + b'\n/old/caf\xe9\n/old/two\n')

    def test_file_that_is_not_a_sidecar_is_left_alone(self):
        for content in ('old line 1\n\nold line 3', 'relative/path\n', '/no/final/newline', '/a\n\n'):
            with self.subTest(content=content):
                keep, dupe = self.write('a/f'), self.write('b/f')
                self.write('b/f.dupes', content)
                result, logs = self.process(keep, [dupe])
                self.assertEqual(result, (0, 1))
                self.assertTrue(os.path.exists(dupe))
                self.assertEqual(self.read('b/f.dupes'), content.encode())
                self.assertIn('does not look like a sidecar file', logs)

    def test_dry_run_changes_nothing(self):
        keep, dupe = self.write('a/f'), self.write('b/f')
        self.write('b/f.dupes', '/old/path\n')
        before = self.snapshot()
        report = []
        scan = jdupes_sidecar.snapshot([keep, dupe])
        result = jdupes_sidecar.process_set(keep, [dupe], self.args(dry_run=True), report, scan)
        self.assertEqual(result, (1, 0))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(report, [
            f"Would keep file: {keep}",
            f"Would delete duplicate file: {dupe}",
            f"Would merge existing sidecar file: {dupe}.dupes into {keep}.dupes",
            f"Would delete sidecar file: {dupe}.dupes",
            f"Would create sidecar file: {keep}.dupes with contents:",
            f"  {dupe}",
            "  /old/path",
            "",
        ])


class SignalsDeferredTest(unittest.TestCase):
    def test_ctrl_c_waits_for_the_block(self):
        finished = False
        with self.assertRaises(KeyboardInterrupt):
            with jdupes_sidecar.signals_deferred():
                os.kill(os.getpid(), signal.SIGINT)
                finished = True
        self.assertTrue(finished)
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)

    def test_no_signal(self):
        with jdupes_sidecar.signals_deferred():
            pass
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)


@unittest.skipUnless(JDUPES, 'jdupes is not installed')
class EndToEndTest(TempDirTestCase):
    def run_script(self, *args, stdin_text=None):
        result = subprocess.run(
            [sys.executable, SCRIPT, '--jdupes-path', JDUPES] + list(args),
            cwd=self.tmp, input=stdin_text, stdin=None if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result.output = (result.stdout + result.stderr).decode(errors='backslashreplace')
        return result

    def assertExists(self, name):
        self.assertTrue(os.path.lexists(self.path(name)), f"{name} should exist")

    def assertNotExists(self, name):
        self.assertFalse(os.path.lexists(self.path(name)), f"{name} should not exist")

    def assertSidecar(self, name, *entries):
        expected = b''.join(os.fsencode(self.path(e) if not e.startswith('/') else e) + b'\n' for e in entries)
        self.assertEqual(self.read(name), expected)

    def test_keeps_the_copy_in_the_first_directory(self):
        self.write('a/f')
        self.write('b/f')
        self.write('b/sub/g')
        self.write('a/unique', 'unique\n')
        result = self.run_script('--yes', 'b', 'a')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertExists('b/f')
        self.assertNotExists('b/sub/g')
        self.assertNotExists('a/f')
        self.assertExists('a/unique')
        self.assertSidecar('b/f.dupes', 'b/sub/g', 'a/f')
        self.assertIn('Found 1 set of duplicates: 2 files to delete', result.output)

    def test_overlapping_directories_keep_one_copy(self):
        self.write('a/b/x')
        self.write('a/b/z')
        result = self.run_script('--yes', 'a', 'a/b')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertExists('a/b/x')
        self.assertNotExists('a/b/z')
        self.assertSidecar('a/b/x.dupes', 'a/b/z')

    def test_same_directory_twice_keeps_one_copy(self):
        self.write('d/p1')
        self.write('d/p2')
        result = self.run_script('--yes', 'd', 'd/')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertExists('d/p1')
        self.assertNotExists('d/p2')

    def test_merges_and_deletes_sidecars_of_deleted_duplicates(self):
        self.write('a/f')
        self.write('b/f')
        self.write('b/f.dupes', '/old/one\n/old/two\n')
        result = self.run_script('--yes', 'a', 'b')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertSidecar('a/f.dupes', 'b/f', '/old/one', '/old/two')
        self.assertNotExists('b/f.dupes')

    def test_no_delete_duplicate_sidecar(self):
        self.write('a/f')
        self.write('b/f')
        self.write('b/f.dupes', '/old/one\n')
        result = self.run_script('--yes', '--no-delete-duplicate-sidecar', 'a', 'b')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertSidecar('a/f.dupes', 'b/f', '/old/one')
        self.assertExists('b/f.dupes')

    def test_no_merge_existing_sidecars(self):
        self.write('a/f')
        self.write('b/f')
        self.write('b/f.dupes', '/old/one\n')
        result = self.run_script('--yes', '--no-merge-existing-sidecars', 'a', 'b')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertSidecar('a/f.dupes', 'b/f')
        self.assertEqual(self.read('b/f.dupes'), b'/old/one\n')

    def test_appends_to_existing_sidecar(self):
        self.write('a/f')
        self.write('a/f.dupes', '/older\n')
        self.write('b/f')
        result = self.run_script('--yes', 'a', 'b')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertSidecar('a/f.dupes', '/older', 'b/f')

    def test_sidecar_files_are_not_scanned(self):
        self.write('a/x', 'x\n')
        self.write('b/y', 'y\n')
        self.write('a/x.dupes', '/same\n')
        self.write('b/y.dupes', '/same\n')
        result = self.run_script('--yes', 'a', 'b')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertIn('No duplicates found.', result.output)
        self.assertExists('b/y.dupes')

    def test_custom_sidecar_extension(self):
        self.write('a/f')
        self.write('b/f')
        result = self.run_script('--yes', '--sidecar-extension', 'deleted', 'a', 'b')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertSidecar('a/f.deleted', 'b/f')

    def test_dry_run_changes_nothing(self):
        self.write('a/f')
        self.write('b/f')
        self.write('b/f.dupes', '/old/one\n')
        before = self.snapshot()
        result = self.run_script('--dry-run', '-o', '-', 'a', 'b')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(f"Would keep file: {self.path('a/f')}", result.output)
        self.assertIn(f"Would delete duplicate file: {self.path('b/f')}", result.output)
        self.assertIn('Would delete 1 duplicate file.', result.output)

    def test_dry_run_report_file(self):
        self.write('a/f')
        self.write('b/f')
        result = self.run_script('-n', 'a', 'b')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertIn(f"Would delete duplicate file: {self.path('b/f')}".encode(), self.read('dry_run_output.txt'))
        self.assertExists('b/f')

    def test_names_that_are_not_utf8(self):
        self.write(b'a/caf\xe9')
        self.write(b'b/bad\xffname')
        result = self.run_script('--yes', 'a', 'b')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertNotExists(b'b/bad\xffname')
        self.assertEqual(self.read(b'a/caf\xe9.dupes'), self.path(b'b/bad\xffname') + b'\n')

    def test_names_that_are_not_utf8_in_report(self):
        self.write(b'a/caf\xe9')
        self.write(b'b/bad\xffname')
        result = self.run_script('-n', '-o', '-', 'a', 'b')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertIn(b'Would delete duplicate file: ' + self.path(b'b/bad\xffname'), result.stdout)

    def test_duplicate_with_newline_in_name_is_not_deleted(self):
        self.write('a/f')
        self.write('b/new\nline')
        result = self.run_script('--yes', 'a', 'b')
        self.assertEqual(result.returncode, 1, result.output)
        self.assertExists('b/new\nline')
        self.assertIn('contains a newline', result.output)

    def test_unreadable_duplicate_sidecar_keeps_the_duplicate(self):
        self.write('a/f')
        self.write('b/f')
        os.mkdir(self.path('b/f.dupes'))
        result = self.run_script('--yes', 'a', 'b')
        self.assertEqual(result.returncode, 1, result.output)
        self.assertExists('b/f')
        self.assertNotExists('a/f.dupes')

    def test_unwritable_sidecar_deletes_nothing(self):
        self.write('a/f')
        self.write('b/f')
        os.mkdir(self.path('a/f.dupes'))
        result = self.run_script('--yes', 'a', 'b')
        self.assertEqual(result.returncode, 1, result.output)
        self.assertExists('b/f')
        self.assertIn('cannot open sidecar file', result.output)

    def test_hard_links_are_not_deleted(self):
        self.write('a/f')
        os.link(self.path('a/f'), self.path('a/g'))
        self.write('b/f')
        result = self.run_script('--yes', '--jdupes-extra-args=-H', 'a', 'b')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertExists('a/g')
        self.assertNotExists('b/f')
        self.assertSidecar('a/f.dupes', 'b/f')

    def test_asks_for_confirmation(self):
        self.write('a/f')
        self.write('b/f')
        result = self.run_script('a', 'b', stdin_text=b'n\n')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertIn('cancelled', result.output)
        self.assertExists('b/f')
        result = self.run_script('a', 'b', stdin_text=b'y\n')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertNotExists('b/f')

    def test_closed_stdin_without_yes_deletes_nothing(self):
        self.write('a/f')
        self.write('b/f')
        result = self.run_script('a', 'b')
        self.assertEqual(result.returncode, 1, result.output)
        self.assertIn('--yes', result.output)
        self.assertExists('b/f')

    def test_forbidden_extra_args_are_rejected(self):
        self.write('a/f')
        self.write('b/f')
        result = self.run_script('--yes', '--jdupes-extra-args=-dN', 'a', 'b')
        self.assertEqual(result.returncode, 2, result.output)
        self.assertIn('--delete', result.output)
        self.assertExists('b/f')

    def test_extra_args_cannot_smuggle_options_to_jdupes(self):
        self.write('a/f')
        self.write('b/f')
        for extra in ('--chunk -o -dN', '-C -o -dN', '-b -o -dN', '-C'):
            with self.subTest(extra=extra):
                result = self.run_script('--yes', '-n', f'--jdupes-extra-args={extra}', 'a', 'b')
                self.assertEqual(result.returncode, 2, result.output)
                self.assertExists('b/f')

    def test_jdupes_help_output_is_an_error(self):
        self.write('a/f')
        self.write('b/f')
        result = self.run_script('--yes', '--jdupes-extra-args=-X help', 'a', 'b')
        self.assertEqual(result.returncode, 1, result.output)
        self.assertIn('Unexpected output from jdupes', result.output)
        self.assertExists('b/f')

    def test_extra_args_reach_jdupes(self):
        self.write('a/f', '')
        self.write('b/f', '')
        result = self.run_script('--yes', 'a', 'b')
        self.assertIn('No duplicates found.', result.output)
        result = self.run_script('--yes', '--jdupes-extra-args=--zero-match', 'a', 'b')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertNotExists('b/f')

    def test_directory_starting_with_dash(self):
        self.write('-a/f')
        self.write('b/f')
        result = self.run_script('--yes', '--', '-a', 'b')
        self.assertEqual(result.returncode, 0, result.output)
        self.assertNotExists('b/f')

    def test_dot_dot_after_a_symlink(self):
        # 'lnk/../b' is mnt/data/b, not work/b: the kernel follows the symlink before '..'.
        self.write('work/a/f', 'photo-v1\n')
        self.write('mnt/data/b/f', 'photo-v1\n')
        self.write('work/b/f', 'photo-v2\n')
        os.makedirs(self.path('mnt/data/sub'))
        os.symlink('../mnt/data/sub', self.path('work/lnk'))
        result = subprocess.run([sys.executable, SCRIPT, '--jdupes-path', JDUPES, '--yes', 'a', 'lnk/../b'],
                                cwd=self.path('work'), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.read('work/b/f'), b'photo-v2\n')
        self.assertNotExists('mnt/data/b/f')
        self.assertSidecar('work/a/f.dupes', 'mnt/data/b/f')

    def test_sidecar_name_of_kept_file_is_a_duplicate(self):
        self.write('d/report')
        self.write('d/report.bak')
        result = self.run_script('--yes', '--sidecar-extension=bak', '--no-exclude-sidecar', 'd')
        self.assertEqual(result.returncode, 1, result.output)
        self.assertEqual(self.read('d/report.bak'), b'same content\n')
        self.assertExists('d/report')

    def test_sidecar_name_of_duplicate_is_the_kept_file(self):
        self.write('d/report')
        self.write('d/report.bak')
        result = self.run_script('--yes', '--sidecar-extension=bak', '--no-exclude-sidecar',
                                 '--jdupes-extra-args=--reverse', 'd')
        self.assertEqual(result.returncode, 1, result.output)
        self.assertEqual(self.read('d/report.bak'), b'same content\n')
        self.assertExists('d/report')

    def test_sidecar_name_of_duplicate_is_in_another_set(self):
        self.write('a/f', 'F\n')
        self.write('b/f', 'F\n')
        self.write('b/f.bak', 'OLD\n')
        self.write('c/g', 'OLD\n')
        result = self.run_script('--yes', '--sidecar-extension=bak', '--no-exclude-sidecar', 'a', 'b', 'c')
        self.assertEqual(result.returncode, 1, result.output)
        self.assertEqual(self.read('b/f.bak'), b'OLD\n')
        self.assertNotExists('c/g')
        self.assertExists('b/f')

    def test_change_while_waiting_for_confirmation(self):
        self.write('a/doc', 'draft-1\n')
        self.write('b/doc', 'draft-1\n')
        process = subprocess.Popen([sys.executable, SCRIPT, '--jdupes-path', JDUPES, 'a', 'b'], cwd=self.tmp,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = b''
        while not output.endswith(b'[y/N]: '):
            data = process.stdout.read(1)
            self.assertTrue(data, output)
            output += data
        mtime = os.stat(self.path('b/doc')).st_mtime_ns
        self.write('b/doc', 'final-2\n')
        os.utime(self.path('b/doc'), ns=(mtime + 10 ** 9, mtime + 10 ** 9))
        output += process.communicate(b'y\n')[0]
        self.assertEqual(process.returncode, 1, output)
        self.assertEqual(self.read('b/doc'), b'final-2\n')
        self.assertIn(b'changed after the scan', output)

    def test_missing_directory(self):
        result = self.run_script('--yes', 'nonexistent')
        self.assertEqual(result.returncode, 1, result.output)
        self.assertIn('Not a directory: nonexistent', result.output)

    def test_file_arguments_are_rejected(self):
        # jdupes would skip the file with a warning and go on with the other directories.
        self.write('f')
        self.write('b/f')
        self.write('c/f')
        result = self.run_script('--yes', 'f', 'b', 'c')
        self.assertEqual(result.returncode, 1, result.output)
        self.assertIn('Not a directory: f', result.output)
        self.assertExists('c/f')

    def test_jdupes_not_found(self):
        self.write('a/f')
        result = subprocess.run([sys.executable, SCRIPT, '--yes', '--jdupes-path', self.path('nope'), 'a'],
                                cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'Cannot run jdupes', result.stdout)


if __name__ == '__main__':
    unittest.main()
