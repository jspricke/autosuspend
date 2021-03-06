#!/usr/bin/env python3
"""A daemon to suspend a system on inactivity."""

import argparse
import configparser
import datetime
import functools
import logging
import logging.config
import os
import os.path
import subprocess
import time
from typing import (Callable,
                    IO,
                    Iterable,
                    List,
                    Optional,
                    Sequence,
                    Type,
                    TypeVar)

from .checks import (Activity,
                     Check,
                     ConfigurationError,
                     TemporaryCheckError,
                     Wakeup)
from .util import logger_by_class_instance


# pylint: disable=invalid-name
_logger = logging.getLogger('autosuspend')
# pylint: enable=invalid-name


def execute_suspend(command: str, wakeup_at: Optional[datetime.datetime]):
    """Suspend the system by calling the specified command.

    Args:
        command:
            The command to execute, which will be executed using shell
            execution
        wakeup_at:
            potential next wakeup time. Only informative.
    """
    _logger.info('Suspending using command: %s', command)
    try:
        subprocess.check_call(command, shell=True)
    except subprocess.CalledProcessError:
        _logger.warning('Unable to execute suspend command: %s', command,
                        exc_info=True)


def notify_suspend(command_wakeup_template: Optional[str],
                   command_no_wakeup: Optional[str],
                   wakeup_at: Optional[datetime.datetime]):
    """Call a command to notify on suspending.

    Args:
        command_no_wakeup_template:
            A template for the command to execute in case a wakeup is
            scheduled.
            It will be executed using shell execution.
            The template is processed with string formatting to include
            information on a potentially scheduled wakeup.
            Notifications can be disable by providing ``None`` here.
        command_no_wakeup:
            Command to execute for notification in case no wake up is
            scheduled.
            Will be executed using shell execution.
        wakeup_at:
            if not ``None``, this is the time the system will wake up again
    """

    def safe_exec(command):
        _logger.info('Notifying using command: %s', command)
        try:
            subprocess.check_call(command, shell=True)
        except subprocess.CalledProcessError:
            _logger.warning('Unable to execute notification command: %s',
                            command, exc_info=True)

    if wakeup_at and command_wakeup_template:
        command = command_wakeup_template.format(
            timestamp=wakeup_at.timestamp(),
            iso=wakeup_at.isoformat())
        safe_exec(command)
    elif not wakeup_at and command_no_wakeup:
        safe_exec(command_no_wakeup)
    else:
        _logger.info('No suitable notification command configured.')


def notify_and_suspend(suspend_cmd: str,
                       notify_cmd_wakeup_template: Optional[str],
                       notify_cmd_no_wakeup: Optional[str],
                       wakeup_at: Optional[datetime.datetime]):
    notify_suspend(notify_cmd_wakeup_template, notify_cmd_no_wakeup, wakeup_at)
    execute_suspend(suspend_cmd, wakeup_at)


def schedule_wakeup(command_template: str, wakeup_at: datetime.datetime):
    command = command_template.format(timestamp=wakeup_at.timestamp(),
                                      iso=wakeup_at.isoformat())
    _logger.info('Scheduling wakeup using command: %s', command)
    try:
        subprocess.check_call(command, shell=True)
    except subprocess.CalledProcessError:
        _logger.warning('Unable to execute wakeup scheduling command: %s',
                        command, exc_info=True)


def execute_checks(checks: Iterable[Activity],
                   all_checks: bool,
                   logger) -> bool:
    """Execute the provided checks sequentially.

    Args:
        checks:
            the checks to execute
        all_checks:
            if ``True``, execute all checks even if a previous one already
            matched.

    Return:
        ``True`` if a check matched
    """
    matched = False
    for check in checks:
        logger.debug('Executing check %s', check.name)
        try:
            result = check.check()
            if result is not None:
                logger.info('Check %s matched. Reason: %s', check.name, result)
                matched = True
                if not all_checks:
                    logger.debug('Skipping further checks')
                    break
        except TemporaryCheckError:
            logger.warning('Check %s failed. Ignoring...', check,
                           exc_info=True)
    return matched


def execute_wakeups(wakeups: Iterable[Wakeup],
                    timestamp: datetime.datetime,
                    logger) -> Optional[datetime.datetime]:

    wakeup_at = None
    for wakeup in wakeups:
        try:
            this_at = wakeup.check(timestamp)

            # sanity checks
            if this_at is None:
                continue
            if this_at <= timestamp:
                logger.warning('Wakeup %s returned a scheduled wakeup at %s, '
                               'which is earlier than the current time %s. '
                               'Ignoring.',
                               wakeup, this_at, timestamp)
                continue

            if wakeup_at is None:
                wakeup_at = this_at
            else:
                wakeup_at = min(this_at, wakeup_at)
        except TemporaryCheckError:
            logger.warning('Wakeup %s failed. Ignoring...', wakeup,
                           exc_info=True)

    return wakeup_at


class Processor:
    """Implements the logic for triggering suspension.

    Args:
        activities:
            the activity checks to execute
        wakeups:
            the wakeup checks to execute
        idle_time:
            the required amount of time the system has to be idle before
            suspension is triggered in seconds
        min_sleep_time:
            the minimum time the system has to sleep before it is woken up
            again in seconds.
        wakeup_delta:
            wake up this amount of seconds before the scheduled wake up time.
        sleep_fn:
            a callable that triggers suspension
        wakeup_fn:
            a callable that schedules the wakeup at the specified time in UTC
            seconds
        notify_fn:
            a callable that is called before suspending.
            One argument gives the scheduled wakeup time or ``None``.
        all_activities:
            if ``True``, execute all activity checks even if a previous one
            already matched.
    """

    def __init__(self,
                 activities: List[Activity],
                 wakeups: List[Wakeup],
                 idle_time: float,
                 min_sleep_time: float,
                 wakeup_delta: float,
                 sleep_fn: Callable,
                 wakeup_fn: Callable[[datetime.datetime], None],
                 all_activities: bool) -> None:
        self._logger = logger_by_class_instance(self)
        self._activities = activities
        self._wakeups = wakeups
        self._idle_time = idle_time
        self._min_sleep_time = min_sleep_time
        self._wakeup_delta = wakeup_delta
        self._sleep_fn = sleep_fn
        self._wakeup_fn = wakeup_fn
        self._all_activities = all_activities
        self._idle_since = None  # type: Optional[datetime.datetime]

    def _reset_state(self, reason: str) -> None:
        self._logger.info('%s. Resetting state', reason)
        self._idle_since = None

    def iteration(self, timestamp: datetime.datetime, just_woke_up: bool):
        self._logger.info('Starting new check iteration')

        # determine system activity
        active = execute_checks(self._activities, self._all_activities,
                                self._logger)
        self._logger.debug('All activity checks have been executed. '
                           'Active: %s', active)
        # determine potential wake ups
        wakeup_at = execute_wakeups(self._wakeups, timestamp, self._logger)
        self._logger.debug('Checks report, system should wake up at %s',
                           wakeup_at)
        if wakeup_at is not None:
            wakeup_at -= datetime.timedelta(seconds=self._wakeup_delta)
        self._logger.debug('With delta, system should wake up at %s',
                           wakeup_at)

        # exit in case something prevents suspension
        if just_woke_up:
            self._reset_state('Just woke up from suspension')
            return
        if active:
            self._reset_state('System is active')
            return

        # set idle timestamp if required
        if self._idle_since is None:
            self._idle_since = timestamp

        self._logger.info('System is idle since %s', self._idle_since)

        # determine if systems is idle long enough
        self._logger.debug('Idle seconds: %s',
                           (timestamp - self._idle_since).total_seconds())
        if (timestamp - self._idle_since).total_seconds() > self._idle_time:
            self._logger.info('System is idle long enough.')

            # idle time would be reached, handle wake up
            if wakeup_at is not None:
                wakeup_in = wakeup_at - timestamp
                if wakeup_in.total_seconds() < self._min_sleep_time:
                    self._logger.info('Would wake up in %s seconds, which is '
                                      'below the minimum amount of %s s. '
                                      'Not suspending.',
                                      wakeup_in.total_seconds(),
                                      self._min_sleep_time)
                    return

                # schedule wakeup
                self._logger.info('Scheduling wakeup at %s', wakeup_at)
                self._wakeup_fn(wakeup_at)

            self._reset_state('Going to suspend')
            self._sleep_fn(wakeup_at)
        else:
            self._logger.info('Desired idle time of %s s not reached yet.',
                              self._idle_time)


def loop(processor: Processor,
         interval: int,
         run_for: Optional[int],
         woke_up_file: str) -> None:
    """Run the main loop of the daemon.

    Args:
        processor:
            the processor to use for handling the suspension computations
        interval:
            the length of one iteration of the main loop in seconds
        idle_time:
            the required amount of time the system has to be idle before
            suspension is triggered
        sleep_fn:
            a callable that triggers suspension
        run_for:
            if specified, run the main loop for the specified amount of seconds
            before terminating (approximately)
    """

    start_time = datetime.datetime.now(datetime.timezone.utc)
    while (run_for is None) or (datetime.datetime.now(datetime.timezone.utc) <
                                (start_time + datetime.timedelta(
                                    seconds=run_for))):

        just_woke_up = os.path.isfile(woke_up_file)
        if just_woke_up:
            os.remove(woke_up_file)

        processor.iteration(datetime.datetime.now(datetime.timezone.utc),
                            just_woke_up)

        time.sleep(interval)


CheckType = TypeVar('CheckType', bound=Check)


def set_up_checks(config: configparser.ConfigParser,
                  prefix: str,
                  internal_module: str,
                  target_class: Type[CheckType],
                  error_none: bool = False) -> List[CheckType]:
    """Set up :py.class:`Check` instances from a given configuration.

    Args:
        config:
            the configuration to use
        prefix:
            The prefix of sections in the configuration file to use for
            creating instances.
        internal_module:
            Name of the submodule of ``autosuspend.checks`` to use for
            discovering internal check classes.
        target_class:
            the base class to check new instance against
        error_none:
            Raise an error if nothing was configured?
    """
    configured_checks = []  # type: List[CheckType]

    check_section = [s for s in config.sections()
                     if s.startswith('{}.'.format(prefix))]
    for section in check_section:
        name = section[len('{}.'.format(prefix)):]
        # legacy method to determine the check name from the section header
        class_name = name
        # if there is an explicit class, use that one with higher priority
        if 'class' in config[section]:
            class_name = config[section]['class']
        enabled = config.getboolean(section, 'enabled', fallback=False)

        if not enabled:
            _logger.debug('Skipping disabled check {}'.format(name))
            continue

        # try to find the required class
        if '.' in class_name:
            # dot in class name means external class
            import_module, import_class = class_name.rsplit('.', maxsplit=1)
        else:
            # no dot means internal class
            import_module = 'autosuspend.checks.{}'.format(internal_module)
            import_class = class_name
        _logger.info(
            'Configuring check {} with class {} from module {} '
            'using config section items {}'.format(
                name, import_class, import_module,
                dict(config[section].items())))
        try:
            klass = getattr(__import__(import_module, fromlist=[import_class]),
                            import_class)
        except AttributeError as error:
            raise ConfigurationError(
                'Cannot create built-in check named {}: '
                'Class does not exist'.format(class_name)) from error

        check = klass.create(name, config[section])
        if not isinstance(check, target_class):
            raise ConfigurationError(
                'Check {} is not a correct {} instance'.format(
                    check, target_class.__name__))
        _logger.debug('Created check instance {} with options {}'.format(
            check, check.options()))
        configured_checks.append(check)

    if not configured_checks and error_none:
        raise ConfigurationError('No checks enabled')

    return configured_checks


def parse_config(config_file: Iterable[str]):
    """Parse the configuration file.

    Args:
        config_file:
            The file to parse
    """
    _logger.debug('Reading config file %s', config_file)
    config = configparser.ConfigParser(
        interpolation=configparser.ExtendedInterpolation())
    config.read_file(config_file)
    _logger.debug('Parsed config file: %s', config)
    return config


def parse_arguments(args: Optional[Sequence[str]]) -> argparse.Namespace:
    """Parse command line arguments.

    Args:
        args:
            if specified, use the provided arguments instead of the default
            ones determined via the :module:`sys` module.
    """
    parser = argparse.ArgumentParser(
        description='Automatically suspends a server '
                    'based on several criteria',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    IO  # for making pyflakes happy
    default_config = None  # type: Optional[IO[str]]
    try:
        default_config = open('/etc/autosuspend.conf', 'r')
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        pass
    parser.add_argument(
        '-c', '--config',
        dest='config_file',
        type=argparse.FileType('r'),
        default=default_config,
        required=default_config is None,
        metavar='FILE',
        help='The config file to use')
    parser.add_argument(
        '-a', '--allchecks',
        dest='all_checks',
        default=False,
        action='store_true',
        help='Execute all checks even if one has already prevented '
             'the system from going to sleep. Useful to debug individual '
             'checks.')
    parser.add_argument(
        '-r', '--runfor',
        dest='run_for',
        type=float,
        default=None,
        metavar='SEC',
        help="If set, run for the specified amount of seconds before exiting "
             "instead of endless execution.")
    parser.add_argument(
        '-l', '--logging',
        type=argparse.FileType('r'),
        nargs='?',
        default=False,
        const=True,
        metavar='FILE',
        help='Configures the python logging system. If used '
             'without an argument, all logging is enabled to '
             'the console. If used with an argument, the '
             'configuration is read from the specified file.')

    result = parser.parse_args(args)

    _logger.debug('Parsed command line arguments %s', result)

    return result


def configure_logging(file_or_flag):
    """Configure the python :mod:`logging` system.

    If the provided argument is a `file` instance, try to use the
    pointed to file as a configuration for the logging system. Otherwise,
    if the given argument evaluates to :class:True:, use a default
    configuration with many logging messages. If everything fails, just log
    starting from the warning level.

    Args:
        file_or_flag (file or bool):
            either a configuration file pointed by a :ref:`file object
            <python:bltin-file-objects>` instance or something that evaluates
            to :class:`bool`.
    """
    if isinstance(file_or_flag, bool):
        if file_or_flag:
            logging.basicConfig(level=logging.DEBUG)
        else:
            # at least configure warnings
            logging.basicConfig(level=logging.WARNING)
    else:
        try:
            logging.config.fileConfig(file_or_flag)
        except Exception:
            # at least configure warnings
            logging.basicConfig(level=logging.WARNING)
            _logger.warning('Unable to configure logging from file %s. '
                            'Falling back to warning level.',
                            file_or_flag,
                            exc_info=True)


def main(args=None):
    """Run the daemon."""
    args = parse_arguments(args)

    configure_logging(args.logging)

    config = parse_config(args.config_file)

    checks = set_up_checks(config, 'check', 'activity', Activity,
                           error_none=True)
    wakeups = set_up_checks(config, 'wakeup', 'wakeup', Wakeup)

    processor = Processor(
        checks, wakeups,
        config.getfloat('general', 'idle_time', fallback=300),
        config.getfloat('general', 'min_sleep_time', fallback=1200),
        config.getfloat('general', 'wakeup_delta', fallback=30),
        functools.partial(notify_and_suspend,
                          config.get('general', 'suspend_cmd'),
                          config.get('general', 'notify_cmd_wakeup',
                                     fallback=None),
                          config.get('general', 'notify_cmd_no_wakeup',
                                     fallback=None)),
        functools.partial(schedule_wakeup,
                          config.get('general', 'wakeup_cmd')),
        all_activities=args.all_checks)
    loop(processor,
         config.getfloat('general', 'interval', fallback=60),
         run_for=args.run_for,
         woke_up_file=config.get('general', 'woke_up_file',
                                 fallback='/var/run/autosuspend-just-woke-up'))


if __name__ == "__main__":
    main()
