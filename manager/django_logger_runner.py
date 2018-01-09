#!/usr/bin/env python3
"""Run loggers using modes/configurations from the Django database.

Typical use:

  # Create an expanded configuration
  manager/build_config.py --config test/configs/sample_cruise.json > config.json

  # Run the Django test server
  ./manage.py runserver localhost:8000

  # Point your browser at http://localhost:8000/manager, log in and load the
  # configuration you created using the "Choose file" and "Load configuration
  # file" buttons at the bottom of the page.

  # In a separate terminal run this script to monitor the Django db and
  # run loggers as indicated.
  manager/run_loggers_db.py -v

(To get this to work using the sample config sample_cruise.json above,
sample_cruise.json, you'll also need to run

  logger/utils/serial_sim.py --config test/serial_sim.py

in a separate terminal window to create the virtual serial ports the
sample config references and feed simulated data through them.)
"""

import argparse
import logging
import multiprocessing
import os
import pprint
import signal
import sys
import time
import threading

sys.path.append('.')

import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gui.settings')
django.setup()

from manager.models import Logger, Config, Mode, Cruise
from manager.models import ConfigState, CruiseState, CurrentCruise

from logger.utils.read_json import parse_json
from logger.listener.listen import ListenerFromConfig

run_logging = logging.getLogger(__name__)

TIME_FORMAT = '%Y-%m-%d:%H:%M:%S'

################################################################################
class DjangoLoggerRunner:
  ############################
  def __init__(self, interval=0.5):
    """Create a LoggerRunner that reads desired states from Django DB.
    interval - number of seconds to sleep between checking/updating loggers
    """

    """
    # Do some basic checking
    has_modes = config.get('modes', None)
    if not has_modes and type(has_modes) is dict:
      raise ValueError('Improper LoggerRunner config: no "modes" dict found')
    self.configuration = config

    if mode:
      if not type(mode) is str:
        raise ValueError('LoggerRunner mode "%s" must be string' % mode)
      if not mode in has_modes:
        raise ValueError('LoggerRunner mode "%s" not in modes dict' % mode)
    self.mode = mode or config.get('default_mode', None)
    if not self.mode:
      raise ValueError('LoggerRunner no mode specified and no default found')
    """
    
    # Map logger name to config and to the process running it
    self.configs = {}
    self.processes = {}

    self.interval = interval
    self.quit_flag = False
    
  ############################
  def check_loggers(self, halt=False):
    """Start up all the loggers using the configuration specified in
    the current mode."""

    # Build a status dict we'll return at the end for the status
    # server (or whomever) to use.    
    status = {}
    
    # Get the current cruise
    try:
      cruise = CurrentCruise.objects.latest('as_of').cruise
    except CurrentCruise.DoesNotExist:
      logging.warning('No current cruise - nothing to do.')
      return None
    status['cruise'] = cruise.id
    status['cruise_start'] = cruise.start.strftime(TIME_FORMAT)
    status['cruise_end'] = cruise.end.strftime(TIME_FORMAT)

    current_mode = cruise.current_mode
    status['current_mode'] = current_mode.name

    # We'll fill in the logger statuses one at a time
    status['loggers'] = {}

    # Get config corresponding to current mode for each logger
    logger_status = {}
    for logger in Logger.objects.all():
      
      # What config do we want logger to be in?
      try:
        desired_config = Config.objects.get(logger=logger, mode=current_mode)
        logger_status = {
          'desired_config': desired_config.name,
          'desired_config_json': desired_config.config_json,
          'enabled': desired_config.enabled
        }
        logging.info('desired_config for %s is %s',
                     logger.name, desired_config.name)
      except Config.DoesNotExist:
        desired_config = None
        logger_status = {}
        logging.info('no config for %s', logger.name)

      # What config is logger currently in (as far as we know)?
      current_config = self.configs.get(logger, None)
      if current_config:
        logger_status['current_config'] = current_config.name
        logger_status['enabled'] = current_config.enabled

      # We've assembled all the information we need for status. Stash it
      status['loggers'][logger.name] = logger_status

      # Special case escape: if we've been given the 'halt' signal,
      # the desired_config of every logger is "off", represented by
      # None.
      if halt:
        desired_config = None
      
      # Isn't running and shouldn't be running. Nothing to do here
      if current_config is None and desired_config is None:
        continue

      # If current_config == desired_config (and is not None) and the
      # process is running, all is right with the world; skip to next.
      process = self.processes.get(logger)
      if current_config == desired_config:
        if current_config.enabled and process.is_alive():          
          continue

        # Two possibilities here: process is not alive, or config is
        # not enabled. If process is not alive, complain.
        if not process.is_alive():
          warning = 'Process for "%s" unexpectedly dead!' % logger
          run_logging.warning(warning)
          status['loggers'][logger.name]['warnings'] = warning

      # If here, process is either running and shouldn't be, or isn't
      # and should be. Kill off process if it exists.
      #
      # NOTE: not clear that terminate() cleanly shuts down a listener
      if process:
        run_logging.info('Shutting down process for %s', logger)
        process.terminate()
        self.processes[logger] = None

      # Start up a new process in the desired_config
      self.configs[logger] = desired_config
      if desired_config:
        run_logging.info('Starting up new process for %s', logger)
        self.processes[logger] = self._start_logger(desired_config)

    # Finally, when we've looped through all the loggers, return an
    # aggregated status.
    return status
  
  ############################
  def quit(self):
    """Exit the loop and shut down all loggers."""
    self.quit_flag = True

  ############################
  def _start_logger(self, config):
    """Create a new process running a Listener/Logger using the passed
    config, and return the Process object."""

    config_dict = parse_json(config.config_json)
    run_logging.debug('Starting config:\n%s', pprint.pformat(config))
    listener = ListenerFromConfig(config_dict)
    proc = multiprocessing.Process(target=listener.run)
    proc.start()
    return proc

  ############################
  def run(self):
    """Start up all the loggers using the configuration specified in
    the current mode, loop and keep checking them."""

    while not self.quit_flag:

      with self.mode_change_lock:
        # Dict of configs for loggers we're *supposed* to be running
        desired_configs = self.configuration.get('modes').get(self.mode)

      run_logging.info('Checking logger states against mode "%s"', self.mode)
      self._check_loggers(desired_configs)

      run_logging.debug('Sleeping %s seconds...', self.interval)
      time.sleep(self.interval)

      if self.interactive:
        self._get_new_mode()

    # If here, we've dropped out of the "while not quit" loop. Launch
    # a new (empty) desired configuration in which nothing is running
    # prior to exiting.
    run_logging.info('Received quit request - shutting loggers down.')
    self._check_loggers({})

################################################################################
if __name__ == '__main__':
  import argparse
  parser.add_argument('--interval', dest='interval', action='store',
                      type=int, default=1,
                      help='How many seconds to sleep between logger checks.')
  parser.add_argument('-v', '--verbosity', dest='verbosity',
                      default=0, action='count',
                      help='Increase output verbosity')
  parser.add_argument('-V', '--logger_verbosity', dest='logger_verbosity',
                      default=0, action='count',
                      help='Increase output verbosity of component loggers')
  args = parser.parse_args()

  LOGGING_FORMAT = '%(asctime)-15s %(filename)s:%(lineno)d %(message)s'

  logging.basicConfig(format=LOGGING_FORMAT)

  LOG_LEVELS ={0:logging.WARNING, 1:logging.INFO, 2:logging.DEBUG}

  # Our verbosity
  args.verbosity = min(args.verbosity, max(LOG_LEVELS))
  run_logging.setLevel(LOG_LEVELS[args.verbosity])

  # Verbosity of our component loggers (and everything else)
  args.logger_verbosity = min(args.logger_verbosity, max(LOG_LEVELS))
  logging.getLogger().setLevel(LOG_LEVELS[args.logger_verbosity])

  runner = DjangoLoggerRunner(interval=args.interval)
  runner.run()
