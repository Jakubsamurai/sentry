from __future__ import absolute_import

import logging

from sentry.models import Project
from sentry.utils import metrics
from sentry.utils.safe import safe_execute
from collections import namedtuple


logger = logging.getLogger(__name__)


StacktraceInfo = namedtuple('StacktraceInfo', [
    'stacktrace', 'container', 'platforms'])


class StacktraceProcessor(object):

    def __init__(self, data, stacktrace_infos, project=None):
        self.data = data
        self.stacktrace_infos = stacktrace_infos
        if project is None:
            project = Project.objects.get_from_cache(id=data['project'])
        self.project = project

    def close(self):
        pass

    def preprocess_related_data(self):
        return False

    def get_effective_platform(self, frame):
        return frame.get('platform') or self.data['platform']

    def process_frame(self, frame, stacktrace_info, idx):
        pass


def find_stacktraces_in_data(data):
    """Finds all stracktraces in a given data blob and returns it
    together with some meta information.
    """
    rv = []

    def _report_stack(stacktrace, container):
        platforms = set()
        for frame in stacktrace.get('frames') or ():
            platforms.add(frame.get('platform') or data['platform'])
        rv.append(StacktraceInfo(
            stacktrace=stacktrace,
            container=container,
            platforms=platforms
        ))

    exc_container = data.get('sentry.interfaces.Exception')
    if exc_container:
        for exc in exc_container['values']:
            stacktrace = exc.get('stacktrace')
            if stacktrace:
                _report_stack(stacktrace, exc)

    stacktrace = data.get('sentry.interfaces.Stacktrace')
    if stacktrace:
        _report_stack(stacktrace, None)

    threads = data.get('threads')
    if threads:
        for thread in threads['values']:
            stacktrace = thread.get('stacktrace')
            if stacktrace:
                _report_stack(stacktrace, thread)

    return rv


def should_process_for_stacktraces(data):
    from sentry.plugins import plugins
    infos = find_stacktraces_in_data(data)
    platforms = set()
    for info in infos:
        platforms.update(info.platforms or ())
    for plugin in plugins.all(version=2):
        processors = safe_execute(plugin.get_stacktrace_processors,
                                  data=data, stacktrace_infos=infos,
                                  platforms=platforms,
                                  _with_transaction=False)
        if processors:
            return True
    return False


def get_processors_for_stacktraces(data, infos):
    from sentry.plugins import plugins

    platforms = set()
    for info in infos:
        platforms.update(info.platforms or ())

    processors = []
    for plugin in plugins.all(version=2):
        processors.extend(safe_execute(plugin.get_stacktrace_processors,
                                       data=data, stacktrace_infos=infos,
                                       platforms=platforms,
                                       _with_transaction=False) or ())

    if processors:
        project = Project.objects.get_from_cache(id=data['project'])
        processors = [x(data, infos, project) for x in processors]

    return processors


def process_single_stacktrace(stacktrace_info, processors):
    # TODO: associate errors with the frames and processing issues
    changed_raw = False
    changed_processed = False
    raw_frames = []
    processed_frames = []
    all_errors = []

    frame_count = len(stacktrace_info.stacktrace['frames'])
    for idx, frame in enumerate(stacktrace_info.stacktrace['frames']):
        need_processed_frame = True
        need_raw_frame = True
        errors = None
        for processor in processors:
            try:
                rv = processor.process_frame(frame, stacktrace_info,
                                             frame_count - idx - 1)
                if rv is None:
                    continue
            except Exception:
                logger.exception('Failed to process frame')
                continue

            expand_processed, expand_raw, errors = rv or (None, None, None)
            if expand_processed is not None:
                processed_frames.extend(expand_processed)
                changed_processed = True
                need_processed_frame = False

            if expand_raw is not None:
                raw_frames.extend(expand_raw)
                changed_raw = True
                need_raw_frame = False

            break

        if need_processed_frame:
            processed_frames.append(frame)
        if need_raw_frame:
            raw_frames.append(frame)
        all_errors.extend(errors or ())

    return (
        dict(stacktrace_info.stacktrace,
             frames=processed_frames) if changed_processed else None,
        dict(stacktrace_info.stacktrace,
             frames=raw_frames) if changed_raw else None,
        all_errors,
    )


def get_metrics_key(stacktrace_infos):
    platforms = set()
    for info in stacktrace_infos:
        platforms.update(info.platforms)

    if len(platforms) == 1:
        platform = next(iter(platforms))
        if platform == 'javascript':
            return 'sourcemaps.process'
        if platform == 'cocoa':
            return 'dsym.process'
    return 'mixed.process'


def process_stacktraces(data, make_processors=None):
    infos = find_stacktraces_in_data(data)
    if make_processors is None:
        processors = get_processors_for_stacktraces(data, infos)
    else:
        processors = make_processors(data, infos)

    # Early out if we have no processors.  We don't want to record a timer
    # in that case.
    if not processors:
        return

    changed = False

    mkey = get_metrics_key(infos)

    with metrics.timer(mkey, instance=data['project']):
        for processor in processors:
            if processor.preprocess_related_data():
                changed = True

        for stacktrace_info in infos:
            new_stacktrace, raw_stacktrace, errors = process_single_stacktrace(
                stacktrace_info, processors)
            if new_stacktrace is not None:
                stacktrace_info.stacktrace.clear()
                stacktrace_info.stacktrace.update(new_stacktrace)
                changed = True
            if raw_stacktrace is not None and \
               stacktrace_info.container is not None:
                stacktrace_info.container['raw_stacktrace'] = raw_stacktrace
                changed = True
            if errors:
                data.setdefault('errors', []).extend(errors)
                changed = True

        for processor in processors:
            processor.close()

    if changed:
        return data
