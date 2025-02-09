import argparse
import os
import subprocess
import sys
import textwrap
from collections import defaultdict
from contextlib import suppress
from pathlib import Path
from typing import Optional, List

from httpie.manager.cli import parser, missing_subcommand
from httpie.compat import importlib_metadata, get_dist_name
from httpie.context import Environment
from httpie.status import ExitStatus


class PluginInstaller:

    def __init__(self, env: Environment, debug: bool = False) -> None:
        self.env = env
        self.dir = env.config.plugins_dir
        self.debug = debug

        self.setup_plugins_dir()

    def setup_plugins_dir(self) -> None:
        try:
            self.dir.mkdir(
                exist_ok=True,
                parents=True
            )
        except OSError:
            self.env.stderr.write(
                f'Couldn\'t create "{self.dir!s}"'
                ' directory for plugin installation.'
                ' Please re-check the permissions for that directory,'
                ' and if needed, allow write-access.'
            )
            raise

    def fail(
        self,
        command: str,
        target: Optional[str] = None,
        reason: Optional[str] = None
    ) -> ExitStatus:
        message = f'Can\'t {command}'
        if target:
            message += f' {target!r}'
        if reason:
            message += f': {reason}'

        self.env.stderr.write(message + '\n')
        return ExitStatus.ERROR

    def pip(self, *args, **kwargs) -> subprocess.CompletedProcess:
        options = {
            'check': True,
            'shell': False,
            'stdout': self.env.stdout,
            'stderr': subprocess.PIPE,
        }
        options.update(kwargs)

        cmd = [sys.executable, '-m', 'pip', *args]
        return subprocess.run(
            cmd,
            **options
        )

    def install(self, targets: List[str]) -> Optional[ExitStatus]:
        self.env.stdout.write(f"Installing {', '.join(targets)}...\n")
        self.env.stdout.flush()

        try:
            self.pip(
                'install',
                f'--prefix={self.dir}',
                '--no-warn-script-location',
                *targets,
            )
        except subprocess.CalledProcessError as error:
            reason = None
            if error.stderr:
                stderr = error.stderr.decode()

                if self.debug:
                    self.env.stderr.write('Command failed: ')
                    self.env.stderr.write(' '.join(error.cmd) + '\n')
                    self.env.stderr.write(textwrap.indent('  ', stderr))

                last_line = stderr.strip().splitlines()[-1]
                severity, _, message = last_line.partition(': ')
                if severity == 'ERROR':
                    reason = message

            return self.fail('install', ', '.join(targets), reason)

    def _uninstall(self, target: str) -> Optional[ExitStatus]:
        try:
            distribution = importlib_metadata.distribution(target)
        except importlib_metadata.PackageNotFoundError:
            return self.fail('uninstall', target, 'package is not installed')

        base_dir = Path(distribution.locate_file('.')).resolve()
        if self.dir not in base_dir.parents:
            # If the package is installed somewhere else (e.g on the site packages
            # of the real python interpreter), than that means this package is not
            # installed through us.
            return self.fail('uninstall', target,
                             'package is not installed through httpie plugins'
                             ' interface')

        files = distribution.files
        if files is None:
            return self.fail('uninstall', target, 'couldn\'t locate the package')

        # TODO: Consider handling failures here (e.g if it fails,
        # just rever the operation and leave the site-packages
        # in a proper shape).
        for file in files:
            with suppress(FileNotFoundError):
                os.unlink(distribution.locate_file(file))

        metadata_path = getattr(distribution, '_path', None)
        if (
            metadata_path
            and metadata_path.exists()
            and not any(metadata_path.iterdir())
        ):
            metadata_path.rmdir()

        self.env.stdout.write(f'Successfully uninstalled {target}\n')

    def uninstall(self, targets: List[str]) -> ExitStatus:
        # Unfortunately uninstall doesn't work with custom pip schemes. See:
        # - https://github.com/pypa/pip/issues/5595
        # - https://github.com/pypa/pip/issues/4575
        # so we have to implement our own uninstalling logic. Which works
        # on top of the importlib_metadata.

        exit_code = ExitStatus.SUCCESS
        for target in targets:
            exit_code |= self._uninstall(target) or ExitStatus.SUCCESS
        return ExitStatus(exit_code)

    def list(self) -> None:
        from httpie.plugins.registry import plugin_manager

        known_plugins = defaultdict(list)

        for entry_point in plugin_manager.iter_entry_points(self.dir):
            ep_info = (entry_point.group, entry_point.name)
            ep_name = get_dist_name(entry_point) or entry_point.module
            known_plugins[ep_name].append(ep_info)

        for plugin, entry_points in known_plugins.items():
            self.env.stdout.write(plugin)

            version = importlib_metadata.version(plugin)
            if version is not None:
                self.env.stdout.write(f' ({version})')
            self.env.stdout.write('\n')

            for group, entry_point in sorted(entry_points):
                self.env.stdout.write(f'  {entry_point} ({group})\n')

    def run(
        self,
        action: Optional[str],
        args: argparse.Namespace,
    ) -> ExitStatus:
        from httpie.plugins.manager import enable_plugins

        if action is None:
            parser.error(missing_subcommand('plugins'))

        with enable_plugins(self.dir):
            if action == 'install':
                status = self.install(args.targets)
            elif action == 'uninstall':
                status = self.uninstall(args.targets)
            elif action == 'list':
                status = self.list()

        return status or ExitStatus.SUCCESS
