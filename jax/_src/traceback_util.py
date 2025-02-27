# Copyright 2020 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import traceback
import types
from typing import Any, Callable, TypeVar

import jax
from jax._src.lib import xla_extension
from jax._src import util


C = TypeVar("C", bound=Callable[..., Any])

_exclude_paths = [__file__, util.__file__]

def register_exclusion(path):
  _exclude_paths.append(path)

_jax_message_append = (
    'The stack trace below excludes JAX-internal frames.\n'
    'The preceding is the original exception that occurred, unmodified.\n'
    '\n--------------------')

def path_starts_with(path, path_prefix):
  path = os.path.abspath(path)
  path_prefix = os.path.abspath(path_prefix)
  try:
    common = os.path.commonpath([path, path_prefix])
  except ValueError:
    # path and path_prefix are both absolute, the only case will raise a
    # ValueError is different drives.
    # https://docs.python.org/3/library/os.path.html#os.path.commonpath
    return False
  try:
    return common == path_prefix or os.path.samefile(common, path_prefix)
  except OSError:
    # One of the paths may not exist.
    return False

def include_frame(f):
  return not any(path_starts_with(f.f_code.co_filename, path)
                 for path in _exclude_paths)

# When scanning stack traces, we might encounter frames from cpython that are
# removed from printed stack traces, such as frames from parts of importlib. We
# ignore these frames heuristically based on source and name match.
def ignore_known_hidden_frame(f):
  return 'importlib._bootstrap' in f.f_code.co_filename

def add_tracebackhide_to_hidden_frames(tb):
  for f, lineno in traceback.walk_tb(tb):
    if not include_frame(f):
      f.f_locals["__tracebackhide__"] = True

def filter_traceback(tb):
  out = None
  # Scan the traceback and collect relevant frames.
  frames = list(traceback.walk_tb(tb))
  for f, lineno in reversed(frames):
    if include_frame(f):
      out = types.TracebackType(out, f, f.f_lasti, lineno)  # pytype: disable=wrong-arg-count
  return out

def add_call_stack_frames(tb):
  # Continue up the call stack.
  #
  # We would like to avoid stepping too far up, e.g. past the exec/eval point of
  # a REPL such as IPython. To that end, we stop past the first contiguous bunch
  # of module-level frames, if we reach any such frames at all. This is a
  # heuristic that might stop in advance of the REPL boundary. For example, if
  # the call stack includes module-level frames from the current module A, and
  # the current module A was imported from within a function F elsewhere, then
  # the stack trace we produce will be truncated at F's frame.
  out = tb

  reached_module_level = False
  for f, lineno in traceback.walk_stack(tb.tb_frame):
    if ignore_known_hidden_frame(f):
      continue
    if reached_module_level and f.f_code.co_name != '<module>':
      break
    if include_frame(f):
      out = types.TracebackType(out, f, f.f_lasti, lineno)  # pytype: disable=wrong-arg-count
    if f.f_code.co_name == '<module>':
      reached_module_level = True
  return out

def is_reraiser_frame(f):
  return (f.filename == __file__ and
          f.name == 'reraise_with_filtered_traceback')

def is_under_reraiser(e):
  tb = traceback.extract_stack(e.__traceback__.tb_frame)
  return any(is_reraiser_frame(f) for f in tb[:-1])

def format_exception_only(e):
  return ''.join(traceback.format_exception_only(type(e), e)).strip()

class UnfilteredStackTrace(Exception): pass

def running_under_ipython():
  """Returns true if we appear to be in an IPython session."""
  try:
    get_ipython()  # type: ignore
    return True
  except NameError:
    return False

def ipython_supports_tracebackhide():
  """Returns true if the IPython version supports __tracebackhide__."""
  import IPython  # type: ignore
  return IPython.version_info[:2] >= (7, 17)

def filtering_mode():
  mode = jax.config.jax_traceback_filtering
  if mode is None or mode == "auto":
    if (running_under_ipython() and ipython_supports_tracebackhide()):
      mode = "tracebackhide"
    else:
      mode = "remove_frames"
  return mode

def api_boundary(fun: C) -> C:
  '''Wraps ``fun`` to form a boundary for filtering exception tracebacks.

  When an exception occurs below ``fun``, this appends to it a custom
  ``__cause__`` that carries a filtered traceback. The traceback imitates the
  stack trace of the original exception, but with JAX-internal frames removed.

  This boundary annotation works in composition with itself. The topmost frame
  corresponding to an :func:`~api_boundary` is the one below which stack traces
  are filtered. In other words, if ``api_boundary(f)`` calls
  ``api_boundary(g)``, directly or indirectly, the filtered stack trace provided
  is the same as if ``api_boundary(f)`` were to simply call ``g`` instead.

  This annotation is primarily useful in wrapping functions output by JAX's
  transformations. For example, consider ``g = jax.jit(f)``. When ``g`` is
  called, JAX's JIT compilation machinery is invoked, which in turn calls ``f``
  in order to trace and translate it. If the function ``f`` raises an exception,
  the stack unwinds through JAX's JIT internals up to the original call site of
  ``g``. Because the function returned by :func:`~jax.jit` is annotated as an
  :func:`~api_boundary`, such an exception is accompanied by an additional
  traceback that excludes the frames specific to JAX's implementation.
  '''

  @util.wraps(fun)
  def reraise_with_filtered_traceback(*args, **kwargs):
    __tracebackhide__ = True
    try:
      return fun(*args, **kwargs)
    except Exception as e:
      mode = filtering_mode()
      if is_under_reraiser(e) or mode == "off":
        raise
      if mode == "tracebackhide":
        add_tracebackhide_to_hidden_frames(e.__traceback__)
        raise
      assert mode == "remove_frames", mode

      filtered_tb, unfiltered, mode = None, None, None
      try:
        filtered_tb = filter_traceback(e.__traceback__)
        msg = format_exception_only(e)
        msg = f'{msg}\n\n{_jax_message_append}'
        unfiltered = UnfilteredStackTrace(msg)
        unfiltered.with_traceback(add_call_stack_frames(e.__traceback__))
        unfiltered.__context__ = e.__context__
        unfiltered.__cause__ = e.__cause__
        unfiltered.__suppress_context__ = e.__suppress_context__
        e.__context__ = None
        e.__cause__ = unfiltered

        e.__traceback__ = filtered_tb
        # In Python < 3.11, there seems to be no way to alter the currently
        # raised exception traceback, except via the C API. The interpreter
        # keeps a copy of the traceback (exc_traceback) that is separate to the
        # __traceback__ of exc_value. Python 3.11 removes exc_traceback and
        # just setting __traceback__ is enough. Since it is no longer needed,
        # the XLA extension no longer defines a traceback-replacing method at
        # Python 3.11 and onward.
        if hasattr(xla_extension, "replace_thread_exc_traceback"):
          xla_extension.replace_thread_exc_traceback(filtered_tb)
        raise
      finally:
        del filtered_tb
        del unfiltered
        del mode
  return reraise_with_filtered_traceback
