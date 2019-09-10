# Copyright 2017 The Bazel Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The whl modules defines classes for interacting with Python packages."""

import argparse
import email.parser
import json
import os
import pkg_resources
import re
import zipfile


class Wheel(object):

  def __init__(self, path):
    self._path = path

  def path(self):
    return self._path

  def basename(self):
    return os.path.basename(self.path())

  def distribution(self):
    # See https://www.python.org/dev/peps/pep-0427/#file-name-convention
    parts = self.basename().split('-')
    return parts[0]

  def version(self):
    # See https://www.python.org/dev/peps/pep-0427/#file-name-convention
    parts = self.basename().split('-')
    return parts[1]

  def repository_name(self):
    # Returns the canonical name of the Bazel repository for this package.
    canonical = 'pypi__{}_{}'.format(self.distribution(), self.version())
    # Escape any illegal characters with underscore.
    return re.sub('[-.+]', '_', canonical)

  def _dist_info(self):
    # Return the name of the dist-info directory within the .whl file.
    # e.g. google_cloud-0.27.0-py2.py3-none-any.whl ->
    #      google_cloud-0.27.0.dist-info
    return '{}-{}.dist-info'.format(self.distribution(), self.version())

  def metadata(self):
    # Extract the structured data from metadata.json in the WHL's dist-info
    # directory.
    with zipfile.ZipFile(self.path(), 'r') as whl:
      with whl.open(self._dist_info() + '/METADATA') as f:
        return self._parse_metadata(f.read().decode("utf-8"))

  def name(self):
    return self.metadata().get('name')

  def dependencies(self, extra=None):
    """Access the dependencies of this Wheel.

    Args:
      extra: if specified, include the additional dependencies
            of the named "extra".

    Yields:
      the names of requirements from the METADATA
    """
    # TODO(mattmoor): Is there a schema to follow for this?
    dependency_set = set()

    for requirement in self.metadata().get('run_requires'):
      if not extra and requirement.extras:
        # Skip extras if not requested.
        continue
      if extra and requirement.extras and extra not in requirement.extras:
        # Match the requirements for the extra we're looking for.
        continue
      if requirement.marker and not requirement.marker.evaluate({"extra": extra}):
        # The current environment does not match the provided PEP 508 marker,
        # so ignore this requirement.
        continue
      dependency_set.add(requirement.project_name)
    return dependency_set

  def extras(self):
    return self.metadata().get('extras', [])

  def expand(self, directory):
    with zipfile.ZipFile(self.path(), 'r') as whl:
      whl.extractall(directory)

  # _parse_metadata parses METADATA files according to PEP 314, 345, and 566.
  def _parse_metadata(self, content):
    metadata = email.parser.Parser().parsestr(content)
    requirements = list(pkg_resources.parse_requirements(
      metadata.get_all('Requires', []) + metadata.get_all('Requires-Dist', []),
    ))
    # Extras may also appear at the end of a marker:
    # https://github.com/pypa/wheel/blob/c4d2b4b81ba6c2de26da8595c9dd9717964f510c/wheel/metadata.py#L13-L14
    def find_marker_extra(markers):
      if isinstance(markers, list):
        return find_marker_extra(markers[-1])
      elif isinstance(markers, tuple):
        lhs, op, rhs = markers
        if isinstance(lhs, pkg_resources.packaging.markers.Variable) and lhs.value == "extra":
          return rhs.value
      else:
        return None
    extras = set()
    for requirement in requirements:
      if requirement.marker:
        marker_extra = find_marker_extra(requirement.marker._markers)
        if marker_extra:
          requirement.extras += (marker_extra,)
      extras.update(requirement.extras)
    return {
      'name': metadata.get("name"),
      'run_requires': requirements,
      'extras': extras,
    }


parser = argparse.ArgumentParser(
    description='Unpack a WHL file as a py_library.')

parser.add_argument('--whl', action='store',
                    help=('The .whl file we are expanding.'))

parser.add_argument('--requirements', action='store',
                    help='The pip_import from which to draw dependencies.')

parser.add_argument('--directory', action='store', default='.',
                    help='The directory into which to expand things.')

parser.add_argument('--extras', action='append',
                    help='The set of extras for which to generate library targets.')

def main():
  args = parser.parse_args()
  whl = Wheel(args.whl)

  # Extract the files into the current directory
  whl.expand(args.directory)

  with open(os.path.join(args.directory, 'BUILD'), 'w') as f:
    f.write("""
package(default_visibility = ["//visibility:public"])

load("@rules_python//python:defs.bzl", "py_library")
load("{requirements}", "requirement")

py_library(
    name = "pkg",
    srcs = glob(["**/*.py"]),
    data = glob(["**/*"], exclude=["**/*.py", "**/* *", "BUILD", "WORKSPACE"]),
    # This makes this directory a top-level in the python import
    # search path for anything that depends on this.
    imports = ["."],
    deps = [{dependencies}],
)
{extras}""".format(
  requirements=args.requirements,
  dependencies=','.join([
    'requirement("%s")' % d
    for d in whl.dependencies()
  ]),
  extras='\n\n'.join([
    """py_library(
    name = "{extra}",
    deps = [
        ":pkg",{deps}
    ],
)""".format(extra=extra,
            deps=','.join([
                'requirement("%s")' % dep
                for dep in whl.dependencies(extra)
            ]))
    for extra in args.extras or []
  ])))

if __name__ == '__main__':
  main()
