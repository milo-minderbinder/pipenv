import contextlib
import errno
import logging
import os
import posixpath
import re
import shlex
import hashlib
import shutil
import signal
import stat
import subprocess
import sys
import warnings

from contextlib import contextmanager
from distutils.spawn import find_executable
from pathlib import Path
from urllib.parse import urlparse

import crayons
import parse
import toml
import tomlkit

from click import echo as click_echo

from pipenv import environments
from pipenv.exceptions import (
    PipenvCmdError, PipenvUsageError, RequirementError, ResolutionFailure
)
from pipenv.pep508checker import lookup
from pipenv.vendor.packaging.markers import Marker
from pipenv.vendor.urllib3 import util as urllib3_util
from pipenv.vendor.vistir.compat import (
    Mapping, ResourceWarning, Sequence, Set, TemporaryDirectory, lru_cache
)
from pipenv.vendor.vistir.misc import fs_str, run
from pipenv.vendor.vistir.contextmanagers import open_file


if environments.MYPY_RUNNING:
    from typing import Any, Dict, List, Optional, Text, Tuple, Union

    from pipenv.project import Project, TSource
    from pipenv.vendor.requirementslib.models.pipfile import Pipfile
    from pipenv.vendor.requirementslib.models.requirements import (
        Line, Requirement
    )


logging.basicConfig(level=logging.ERROR)

specifiers = [k for k in lookup.keys()]
# List of version control systems we support.
VCS_LIST = ("git", "svn", "hg", "bzr")
SCHEME_LIST = ("http://", "https://", "ftp://", "ftps://", "file://")
requests_session = None  # type: ignore


def _get_requests_session(max_retries=1):
    """Load requests lazily."""
    global requests_session
    if requests_session is not None:
        return requests_session
    import requests

    requests_session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=max_retries)
    requests_session.mount("https://pypi.org/pypi", adapter)
    return requests_session


def cleanup_toml(tml):
    toml = tml.split("\n")
    new_toml = []
    # Remove all empty lines from TOML.
    for line in toml:
        if line.strip():
            new_toml.append(line)
    toml = "\n".join(new_toml)
    new_toml = []
    # Add newlines between TOML sections.
    for i, line in enumerate(toml.split("\n")):
        # Skip the first line.
        if line.startswith("["):
            if i > 0:
                # Insert a newline before the heading.
                new_toml.append("")
        new_toml.append(line)
    # adding new line at the end of the TOML file
    new_toml.append("")
    toml = "\n".join(new_toml)
    return toml


def convert_toml_outline_tables(parsed):
    """Converts all outline tables to inline tables."""
    def convert_tomlkit_table(section):
        if isinstance(section, tomlkit.items.Table):
            body = section.value._body
        else:
            body = section._body
        for key, value in body:
            if not key:
                continue
            if hasattr(value, "keys") and not isinstance(value, tomlkit.items.InlineTable):
                table = tomlkit.inline_table()
                table.update(value.value)
                section[key.key] = table

    def convert_toml_table(section):
        for package, value in section.items():
            if hasattr(value, "keys") and not isinstance(value, toml.decoder.InlineTableDict):
                table = toml.TomlDecoder().get_empty_inline_table()
                table.update(value)
                section[package] = table

    is_tomlkit_parsed = isinstance(parsed, tomlkit.container.Container)
    for section in ("packages", "dev-packages"):
        table_data = parsed.get(section, {})
        if not table_data:
            continue
        if is_tomlkit_parsed:
            convert_tomlkit_table(table_data)
        else:
            convert_toml_table(table_data)

    return parsed


def run_command(cmd, *args, is_verbose=False, **kwargs):
    """
    Take an input command and run it, handling exceptions and error codes and returning
    its stdout and stderr.

    :param cmd: The list of command and arguments.
    :type cmd: list
    :returns: A 2-tuple of the output and error from the command
    :rtype: Tuple[str, str]
    :raises: exceptions.PipenvCmdError
    """

    from ._compat import decode_for_output
    from .cmdparse import Script
    catch_exceptions = kwargs.pop("catch_exceptions", True)
    if isinstance(cmd, ((str,), list, tuple)):
        cmd = Script.parse(cmd)
    if not isinstance(cmd, Script):
        raise TypeError("Command input must be a string, list or tuple")
    if "env" not in kwargs:
        kwargs["env"] = os.environ.copy()
    kwargs["env"]["PYTHONIOENCODING"] = "UTF-8"
    command = [cmd.command, *cmd.args]
    if is_verbose:
        click_echo(f"Running command: $ {cmd.cmdify()}")
    c = subprocess_run(command, *args, **kwargs)
    if is_verbose:
        click_echo("Command output: {}".format(
            crayons.cyan(decode_for_output(c.stdout))
        ), err=True)
    if c.returncode and catch_exceptions:
        raise PipenvCmdError(cmd.cmdify(), c.stdout, c.stderr, c.returncode)
    return c


def parse_python_version(output):
    """Parse a Python version output returned by `python --version`.

    Return a dict with three keys: major, minor, and micro. Each value is a
    string containing a version part.

    Note: The micro part would be `'0'` if it's missing from the input string.
    """
    version_line = output.split("\n", 1)[0]
    version_pattern = re.compile(
        r"""
        ^                   # Beginning of line.
        Python              # Literally "Python".
        \s                  # Space.
        (?P<major>\d+)      # Major = one or more digits.
        \.                  # Dot.
        (?P<minor>\d+)      # Minor = one or more digits.
        (?:                 # Unnamed group for dot-micro.
            \.              # Dot.
            (?P<micro>\d+)  # Micro = one or more digit.
        )?                  # Micro is optional because pypa/pipenv#1893.
        .*                  # Trailing garbage.
        $                   # End of line.
    """,
        re.VERBOSE,
    )

    match = version_pattern.match(version_line)
    if not match:
        return None
    return match.groupdict(default="0")


def python_version(path_to_python):
    from .vendor.pythonfinder.utils import get_python_version

    if not path_to_python:
        return None
    try:
        version = get_python_version(path_to_python)
    except Exception:
        return None
    return version


def escape_grouped_arguments(s):
    """Prepares a string for the shell (on Windows too!)

    Only for use on grouped arguments (passed as a string to Popen)
    """
    if s is None:
        return None

    # Additional escaping for windows paths
    if os.name == "nt":
        s = "{}".format(s.replace("\\", "\\\\"))
    return '"' + s.replace("'", "'\\''") + '"'


def clean_pkg_version(version):
    """Uses pip to prepare a package version string, from our internal version."""
    return pep440_version(str(version).replace("==", ""))


class HackedPythonVersion:
    """A Beautiful hack, which allows us to tell pip which version of Python we're using."""

    def __init__(self, python_version, python_path):
        self.python_version = python_version
        self.python_path = python_path

    def __enter__(self):
        # Only inject when the value is valid
        if self.python_version:
            os.environ["PIPENV_REQUESTED_PYTHON_VERSION"] = str(self.python_version)
        if self.python_path:
            os.environ["PIP_PYTHON_PATH"] = str(self.python_path)

    def __exit__(self, *args):
        # Restore original Python version information.
        try:
            del os.environ["PIPENV_REQUESTED_PYTHON_VERSION"]
        except KeyError:
            pass


def prepare_pip_source_args(sources, pip_args=None):
    if pip_args is None:
        pip_args = []
    if sources:
        # Add the source to notpip.
        package_url = sources[0].get("url")
        if not package_url:
            raise PipenvUsageError("[[source]] section does not contain a URL.")
        pip_args.extend(["-i", package_url])
        # Trust the host if it's not verified.
        if not sources[0].get("verify_ssl", True):
            url_parts = urllib3_util.parse_url(package_url)
            url_port = f":{url_parts.port}" if url_parts.port else ""
            pip_args.extend(
                ["--trusted-host", f"{url_parts.host}{url_port}"]
            )
        # Add additional sources as extra indexes.
        if len(sources) > 1:
            for source in sources[1:]:
                url = source.get("url")
                if not url:  # not harmless, just don't continue
                    continue
                pip_args.extend(["--extra-index-url", url])
                # Trust the host if it's not verified.
                if not source.get("verify_ssl", True):
                    url_parts = urllib3_util.parse_url(url)
                    url_port = f":{url_parts.port}" if url_parts.port else ""
                    pip_args.extend(
                        ["--trusted-host", f"{url_parts.host}{url_port}"]
                    )
    return pip_args


def get_project_index(project, index=None, trusted_hosts=None):
    # type: (Optional[Union[str, TSource]], Optional[List[str]], Optional[Project]) -> TSource
    from .project import SourceNotFound
    if trusted_hosts is None:
        trusted_hosts = []
    if isinstance(index, Mapping):
        return project.find_source(index.get("url"))
    try:
        source = project.find_source(index)
    except SourceNotFound:
        index_url = urllib3_util.parse_url(index)
        src_name = project.src_name_from_url(index)
        verify_ssl = index_url.host not in trusted_hosts
        source = {"url": index, "verify_ssl": verify_ssl, "name": src_name}
    return source


def get_source_list(
    project,  # type: Project
    index=None,  # type: Optional[Union[str, TSource]]
    extra_indexes=None,  # type: Optional[List[str]]
    trusted_hosts=None,  # type: Optional[List[str]]
    pypi_mirror=None,  # type: Optional[str]
):
    # type: (...) -> List[TSource]
    sources = []  # type: List[TSource]
    if index:
        sources.append(get_project_index(project, index))
    if extra_indexes:
        if isinstance(extra_indexes, str):
            extra_indexes = [extra_indexes]
        for source in extra_indexes:
            extra_src = get_project_index(project, source)
            if not sources or extra_src["url"] != sources[0]["url"]:
                sources.append(extra_src)
        else:
            for source in project.pipfile_sources:
                if not sources or source["url"] != sources[0]["url"]:
                    sources.append(source)
    if not sources:
        sources = project.pipfile_sources[:]
    if pypi_mirror:
        sources = [
            create_mirror_source(pypi_mirror) if is_pypi_url(source["url"]) else source
            for source in sources
        ]
    return sources


def get_indexes_from_requirement(req, project, index=None, extra_indexes=None, trusted_hosts=None, pypi_mirror=None):
    # type: (Requirement, Project, Optional[Text], Optional[List[Text]], Optional[List[Text]], Optional[Text]) -> Tuple[TSource, List[TSource], List[Text]]
    index_sources = []  # type: List[TSource]
    if not trusted_hosts:
        trusted_hosts = []  # type: List[Text]
    if extra_indexes is None:
        extra_indexes = []
    project_indexes = project.pipfile_sources[:]
    indexes = []
    if req.index:
        indexes.append(req.index)
    if getattr(req, "extra_indexes", None):
        if not isinstance(req.extra_indexes, list):
            indexes.append(req.extra_indexes)
        else:
            indexes.extend(req.extra_indexes)
    indexes.extend(project_indexes)
    if len(indexes) > 1:
        index, extra_indexes = indexes[0], indexes[1:]
    index_sources = get_source_list(project, index=index, extra_indexes=extra_indexes, trusted_hosts=trusted_hosts, pypi_mirror=pypi_mirror)
    if len(index_sources) > 1:
        index_source, extra_index_sources = index_sources[0], index_sources[1:]
    else:
        index_source, extra_index_sources = index_sources[0], []
    return index_source, extra_index_sources


@lru_cache()
def get_pipenv_sitedir():
    # type: () -> Optional[str]
    import pkg_resources
    site_dir = next(
        iter(d for d in pkg_resources.working_set if d.key.lower() == "pipenv"), None
    )
    if site_dir is not None:
        return site_dir.location
    return None


class HashCacheMixin:

    """Caches hashes of PyPI artifacts so we do not need to re-download them.

    Hashes are only cached when the URL appears to contain a hash in it and the
    cache key includes the hash value returned from the server). This ought to
    avoid issues where the location on the server changes.
    """
    def __init__(self, directory, session):
        self.session = session
        if not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        super().__init__(directory=directory)

    def get_hash(self, link):
        # If there is no link hash (i.e., md5, sha256, etc.), we don't want
        # to store it.
        hash_value = self.get(link.url)
        if not hash_value:
            hash_value = self._get_file_hash(link).encode()
            self.set(link.url, hash_value)
        return hash_value.decode("utf8")

    def _get_file_hash(self, link):
        from pipenv.vendor.pip_shims import shims

        h = hashlib.new(shims.FAVORITE_HASH)
        with open_file(link.url, self.session) as fp:
            for chunk in iter(lambda: fp.read(8096), b""):
                h.update(chunk)
        return ":".join([h.name, h.hexdigest()])


class Resolver:
    def __init__(
        self, constraints, req_dir, project, sources, index_lookup=None,
        markers_lookup=None, skipped=None, clear=False, pre=False
    ):
        self.initial_constraints = constraints
        self.req_dir = req_dir
        self.project = project
        self.sources = sources
        self.resolved_tree = set()
        self.hashes = {}
        self.clear = clear
        self.pre = pre
        self.results = None
        self.markers_lookup = markers_lookup if markers_lookup is not None else {}
        self.index_lookup = index_lookup if index_lookup is not None else {}
        self.skipped = skipped if skipped is not None else {}
        self.markers = {}
        self.requires_python_markers = {}
        self._pip_args = None
        self._constraints = None
        self._parsed_constraints = None
        self._resolver = None
        self._finder = None
        self._session = None
        self._constraint_file = None
        self._pip_options = None
        self._pip_command = None
        self._retry_attempts = 0
        self._hash_cache = None

    def __repr__(self):
        return (
            "<Resolver (constraints={self.initial_constraints}, req_dir={self.req_dir}, "
            "sources={self.sources})>".format(self=self)
        )

    @staticmethod
    @lru_cache()
    def _get_pip_command():
        from pipenv.vendor.pip_shims import shims

        return shims.InstallCommand()

    @property
    def hash_cache(self):
        from pipenv.vendor.pip_shims import shims

        if not self._hash_cache:
            self._hash_cache = type("HashCache", (HashCacheMixin, shims.SafeFileCache), {})(
                os.path.join(self.project.s.PIPENV_CACHE_DIR, "hashes"), self.session
            )
        return self._hash_cache

    @classmethod
    def get_metadata(
        cls,
        deps,  # type: List[str]
        index_lookup,  # type: Dict[str, str]
        markers_lookup,  # type: Dict[str, str]
        project,  # type: Project
        sources,  # type: Dict[str, str]
        req_dir=None,  # type: Optional[str]
        pre=False,  # type: bool
        clear=False,  # type: bool
    ):
        # type: (...) -> Tuple[Set[str], Dict[str, Dict[str, Union[str, bool, List[str]]]], Dict[str, str], Dict[str, str]]
        constraints = set()  # type: Set[str]
        skipped = dict()  # type: Dict[str, Dict[str, Union[str, bool, List[str]]]]
        if index_lookup is None:
            index_lookup = {}
        if markers_lookup is None:
            markers_lookup = {}
        if not req_dir:
            from .vendor.vistir.path import create_tracked_tempdir
            req_dir = create_tracked_tempdir(prefix="pipenv-", suffix="-reqdir")
        transient_resolver = cls(
            [], req_dir, project, sources, index_lookup=index_lookup,
            markers_lookup=markers_lookup, clear=clear, pre=pre
        )
        for dep in deps:
            if not dep:
                continue
            req, req_idx, markers_idx = cls.parse_line(
                dep, index_lookup=index_lookup, markers_lookup=markers_lookup, project=project
            )
            index_lookup.update(req_idx)
            markers_lookup.update(markers_idx)
            # Add dependencies of any file (e.g. wheels/tarballs), source, or local
            # directories into the initial constraint pool to be resolved with the
            # rest of the dependencies, while adding the files/vcs deps/paths themselves
            # to the lockfile directly
            constraint_update, lockfile_update = cls.get_deps_from_req(
                req, resolver=transient_resolver, resolve_vcs=project.s.PIPENV_RESOLVE_VCS
            )
            constraints |= constraint_update
            skipped.update(lockfile_update)
        return constraints, skipped, index_lookup, markers_lookup

    @classmethod
    def parse_line(
        cls,
        line,  # type: str
        index_lookup=None,  # type: Dict[str, str]
        markers_lookup=None,  # type: Dict[str, str]
        project=None  # type: Optional[Project]
    ):
        # type: (...) -> Tuple[Requirement, Dict[str, str], Dict[str, str]]
        from .vendor.requirementslib.models.requirements import Requirement
        from .vendor.requirementslib.models.utils import DIRECT_URL_RE
        if index_lookup is None:
            index_lookup = {}
        if markers_lookup is None:
            markers_lookup = {}
        if project is None:
            from .project import Project
            project = Project()
        index, extra_index, trust_host, remainder = parse_indexes(line)
        line = " ".join(remainder)
        req = None  # type: Requirement
        try:
            req = Requirement.from_line(line)
        except ValueError:
            direct_url = DIRECT_URL_RE.match(line)
            if direct_url:
                line = "{}#egg={}".format(line, direct_url.groupdict()["name"])
                try:
                    req = Requirement.from_line(line)
                except ValueError:
                    raise ResolutionFailure(f"Failed to resolve requirement from line: {line!s}")
            else:
                raise ResolutionFailure(f"Failed to resolve requirement from line: {line!s}")
        if index:
            try:
                index_lookup[req.normalized_name] = project.get_source(
                    url=index, refresh=True).get("name")
            except TypeError:
                pass
        try:
            req.normalized_name
        except TypeError:
            raise RequirementError(req=req)
        # strip the marker and re-add it later after resolution
        # but we will need a fallback in case resolution fails
        # eg pypiwin32
        if req.markers:
            markers_lookup[req.normalized_name] = req.markers.replace('"', "'")
        return req, index_lookup, markers_lookup

    @classmethod
    def get_deps_from_req(cls, req, resolver=None, resolve_vcs=True):
        # type: (Requirement, Optional["Resolver"], bool) -> Tuple[Set[str], Dict[str, Dict[str, Union[str, bool, List[str]]]]]
        from .vendor.requirementslib.models.requirements import Requirement
        from .vendor.requirementslib.models.utils import (
            _requirement_to_str_lowercase_name
        )
        from .vendor.requirementslib.utils import is_installable_dir

        # TODO: this is way too complex, refactor this
        constraints = set()  # type: Set[str]
        locked_deps = dict()  # type: Dict[str, Dict[str, Union[str, bool, List[str]]]]
        if (req.is_file_or_url or req.is_vcs) and not req.is_wheel:
            # for local packages with setup.py files and potential direct url deps:
            if req.is_vcs:
                req_list, lockfile = get_vcs_deps(reqs=[req])
                req = next(iter(req for req in req_list if req is not None), req_list)
                entry = lockfile[pep423_name(req.normalized_name)]
            else:
                _, entry = req.pipfile_entry
            parsed_line = req.req.parsed_line  # type: Line
            setup_info = None  # type: Any
            try:
                name = req.normalized_name
            except TypeError:
                raise RequirementError(req=req)
            setup_info = req.req.setup_info
            setup_info.get_info()
            locked_deps[pep423_name(name)] = entry
            requirements = []
            # Allow users to toggle resolution off for non-editable VCS packages
            # but leave it on for local, installable folders on the filesystem
            if resolve_vcs or (
                req.editable or parsed_line.is_wheel or (
                    req.is_file_or_url and parsed_line.is_local
                    and is_installable_dir(parsed_line.path)
                )
            ):
                requirements = [v for v in getattr(setup_info, "requires", {}).values()]
            for r in requirements:
                if getattr(r, "url", None) and not getattr(r, "editable", False):
                    if r is not None:
                        if not r.url:
                            continue
                        line = _requirement_to_str_lowercase_name(r)
                        new_req, _, _ = cls.parse_line(line)
                        if r.marker and not r.marker.evaluate():
                            new_constraints = {}
                            _, new_entry = req.pipfile_entry
                            new_lock = {
                                pep423_name(new_req.normalized_name): new_entry
                            }
                        else:
                            new_constraints, new_lock = cls.get_deps_from_req(
                                new_req, resolver
                            )
                        locked_deps.update(new_lock)
                        constraints |= new_constraints
                # if there is no marker or there is a valid marker, add the constraint line
                elif r and (not r.marker or (r.marker and r.marker.evaluate())):
                    line = _requirement_to_str_lowercase_name(r)
                    constraints.add(line)
            # ensure the top level entry remains as provided
            # note that we shouldn't pin versions for editable vcs deps
            if not req.is_vcs:
                if req.specifiers:
                    locked_deps[name]["version"] = req.specifiers
                elif parsed_line.setup_info and parsed_line.setup_info.version:
                    locked_deps[name]["version"] = "=={}".format(
                        parsed_line.setup_info.version
                    )
            # if not req.is_vcs:
            locked_deps.update({name: entry})
        else:
            # if the dependency isn't installable, don't add it to constraints
            # and instead add it directly to the lock
            if req and req.requirement and (
                req.requirement.marker and not req.requirement.marker.evaluate()
            ):
                pypi = resolver.finder if resolver else None
                ireq = req.ireq
                best_match = pypi.find_best_candidate(ireq.name, ireq.specifier).best_candidate if pypi else None
                if best_match:
                    ireq.req.specifier = ireq.specifier.__class__(f"=={best_match.version}")
                    hashes = resolver.collect_hashes(ireq) if resolver else []
                    new_req = Requirement.from_ireq(ireq)
                    new_req = new_req.add_hashes(hashes)
                    name, entry = new_req.pipfile_entry
                    locked_deps[pep423_name(name)] = translate_markers(entry)
                    click_echo(
                        "{} doesn't match your environment, "
                        "its dependencies won't be resolved.".format(req.as_line()),
                        err=True
                    )
                else:
                    click_echo(
                        "Could not find a version of {} that matches your environment, "
                        "it will be skipped.".format(req.as_line()),
                        err=True
                    )
                return constraints, locked_deps
            constraints.add(req.constraint_line)
            return constraints, locked_deps
        return constraints, locked_deps

    @classmethod
    def create(
        cls,
        deps,  # type: List[str]
        project,  # type: Project
        index_lookup=None,  # type: Dict[str, str]
        markers_lookup=None,  # type: Dict[str, str]
        sources=None,  # type: List[str]
        req_dir=None,  # type: str
        clear=False,  # type: bool
        pre=False  # type: bool
    ):
        # type: (...) -> "Resolver"
        from pipenv.vendor.vistir.path import create_tracked_tempdir
        if not req_dir:
            req_dir = create_tracked_tempdir(suffix="-requirements", prefix="pipenv-")
        if index_lookup is None:
            index_lookup = {}
        if markers_lookup is None:
            markers_lookup = {}
        if sources is None:
            sources = project.sources
        constraints, skipped, index_lookup, markers_lookup = cls.get_metadata(
            deps, index_lookup, markers_lookup, project, sources, req_dir=req_dir,
            pre=pre, clear=clear
        )
        return Resolver(
            constraints, req_dir, project, sources, index_lookup=index_lookup,
            markers_lookup=markers_lookup, skipped=skipped, clear=clear, pre=pre
        )

    @classmethod
    def from_pipfile(cls, project, pipfile=None, dev=False, pre=False, clear=False):
        # type: (Optional[Project], Optional[Pipfile], bool, bool, bool) -> "Resolver"
        from pipenv.vendor.vistir.path import create_tracked_tempdir
        if not pipfile:
            pipfile = project._pipfile
        req_dir = create_tracked_tempdir(suffix="-requirements", prefix="pipenv-")
        index_lookup, markers_lookup = {}, {}
        deps = set()
        if dev:
            deps.update({req.as_line() for req in pipfile.dev_packages})
        deps.update({req.as_line() for req in pipfile.packages})
        constraints, skipped, index_lookup, markers_lookup = cls.get_metadata(
            list(deps), index_lookup, markers_lookup, project, project.sources,
            req_dir=req_dir, pre=pre, clear=clear
        )
        return Resolver(
            constraints, req_dir, project, project.sources, index_lookup=index_lookup,
            markers_lookup=markers_lookup, skipped=skipped, clear=clear, pre=pre
        )

    @property
    def pip_command(self):
        if self._pip_command is None:
            self._pip_command = self._get_pip_command()
        return self._pip_command

    def prepare_pip_args(self, use_pep517=None, build_isolation=True):
        pip_args = []
        if self.sources:
            pip_args = prepare_pip_source_args(self.sources, pip_args)
        if use_pep517 is False:
            pip_args.append("--no-use-pep517")
        if build_isolation is False:
            pip_args.append("--no-build-isolation")
        if self.pre:
            pip_args.append("--pre")
        pip_args.extend(["--cache-dir", self.project.s.PIPENV_CACHE_DIR])
        return pip_args

    @property
    def pip_args(self):
        use_pep517 = environments.get_from_env("USE_PEP517", prefix="PIP")
        build_isolation = environments.get_from_env("BUILD_ISOLATION", prefix="PIP")
        if self._pip_args is None:
            self._pip_args = self.prepare_pip_args(
                use_pep517=use_pep517, build_isolation=build_isolation
            )
        return self._pip_args

    def prepare_constraint_file(self):
        from pipenv.vendor.vistir.path import create_tracked_tempfile
        constraints_file = create_tracked_tempfile(
            mode="w",
            prefix="pipenv-",
            suffix="-constraints.txt",
            dir=self.req_dir,
            delete=False,
        )
        skip_args = ("build-isolation", "use-pep517", "cache-dir")
        args_to_add = [
            arg for arg in self.pip_args
            if not any(bad_arg in arg for bad_arg in skip_args)
        ]
        if self.sources:
            requirementstxt_sources = " ".join(args_to_add) if args_to_add else ""
            requirementstxt_sources = requirementstxt_sources.replace(" --", "\n--")
            constraints_file.write(f"{requirementstxt_sources}\n")
        constraints = self.initial_constraints
        constraints_file.write("\n".join([c for c in constraints]))
        constraints_file.close()
        return constraints_file.name

    @property
    def constraint_file(self):
        if self._constraint_file is None:
            self._constraint_file = self.prepare_constraint_file()
        return self._constraint_file

    @property
    def pip_options(self):
        if self._pip_options is None:
            pip_options, _ = self.pip_command.parser.parse_args(self.pip_args)
            pip_options.cache_dir = self.project.s.PIPENV_CACHE_DIR
            pip_options.no_python_version_warning = True
            pip_options.no_input = True
            pip_options.progress_bar = "off"
            pip_options.ignore_requires_python = True
            pip_options.pre = self.pre or self.project.settings.get("allow_prereleases", False)
            self._pip_options = pip_options
        return self._pip_options

    @property
    def session(self):
        if self._session is None:
            self._session = self.pip_command._build_session(self.pip_options)
        return self._session

    @property
    def finder(self):
        from pipenv.vendor.pip_shims import shims
        if self._finder is None:
            self._finder = shims.get_package_finder(
                install_cmd=self.pip_command,
                options=self.pip_options,
                session=self.session
            )
        return self._finder

    @property
    def parsed_constraints(self):
        from pipenv.vendor.pip_shims import shims

        if self._parsed_constraints is None:
            self._parsed_constraints = shims.parse_requirements(
                self.constraint_file, finder=self.finder, session=self.session,
                options=self.pip_options
            )
        return self._parsed_constraints

    @property
    def constraints(self):
        from pipenv.patched.notpip._internal.req.constructors import install_req_from_parsed_requirement

        if self._constraints is None:
            self._constraints = [
                install_req_from_parsed_requirement(
                    c, isolated=self.pip_options.build_isolation,
                    use_pep517=self.pip_options.use_pep517, user_supplied=True
                )
                for c in self.parsed_constraints
            ]
        return self._constraints

    @contextlib.contextmanager
    def get_resolver(self, clear=False):
        from pipenv.vendor.pip_shims.shims import (
            WheelCache, get_requirement_tracker, global_tempdir_manager
        )

        with global_tempdir_manager(), get_requirement_tracker() as req_tracker, TemporaryDirectory(suffix="-build", prefix="pipenv-") as directory:
            pip_options = self.pip_options
            finder = self.finder
            wheel_cache = WheelCache(pip_options.cache_dir, pip_options.format_control)
            directory.path = directory.name
            preparer = self.pip_command.make_requirement_preparer(
                temp_build_dir=directory,
                options=pip_options,
                req_tracker=req_tracker,
                session=self.session,
                finder=finder,
                use_user_site=False,
            )
            resolver = self.pip_command.make_resolver(
                preparer=preparer,
                finder=finder,
                options=pip_options,
                wheel_cache=wheel_cache,
                use_user_site=False,
                ignore_installed=True,
                ignore_requires_python=pip_options.ignore_requires_python,
                force_reinstall=pip_options.force_reinstall,
                upgrade_strategy="to-satisfy-only",
                use_pep517=pip_options.use_pep517,
            )
            yield resolver

    def resolve(self):
        from pipenv.vendor.pip_shims.shims import InstallationError
        from pipenv.exceptions import ResolutionFailure

        with temp_environ(), self.get_resolver() as resolver:
            try:
                results = resolver.resolve(self.constraints, check_supported_wheels=False)
            except InstallationError as e:
                raise ResolutionFailure(message=str(e))
            else:
                self.results = set(results.all_requirements)
                self.resolved_tree.update(self.results)
        return self.resolved_tree

    def resolve_constraints(self):
        from .vendor.requirementslib.models.markers import marker_from_specifier
        new_tree = set()
        for result in self.resolved_tree:
            if result.markers:
                self.markers[result.name] = result.markers
            else:
                candidate = self.finder.find_best_candidate(result.name, result.specifier).best_candidate
                if candidate:
                    requires_python = candidate.link.requires_python
                    if requires_python:
                        marker = marker_from_specifier(requires_python)
                        self.markers[result.name] = marker
                        result.markers = marker
                        if result.req:
                            result.req.marker = marker
            new_tree.add(result)
        self.resolved_tree = new_tree

    @classmethod
    def prepend_hash_types(cls, checksums, hash_type):
        cleaned_checksums = set()
        for checksum in checksums:
            if not checksum:
                continue
            if not checksum.startswith(f"{hash_type}:"):
                checksum = f"{hash_type}:{checksum}"
            cleaned_checksums.add(checksum)
        return cleaned_checksums

    def _get_hashes_from_pypi(self, ireq):
        from pipenv.vendor.pip_shims import shims

        pkg_url = f"https://pypi.org/pypi/{ireq.name}/json"
        session = _get_requests_session(self.project.s.PIPENV_MAX_RETRIES)
        try:
            collected_hashes = set()
            # Grab the hashes from the new warehouse API.
            r = session.get(pkg_url, timeout=10)
            api_releases = r.json()["releases"]
            cleaned_releases = {}
            for api_version, api_info in api_releases.items():
                api_version = clean_pkg_version(api_version)
                cleaned_releases[api_version] = api_info
            version = ""
            if ireq.specifier:
                spec = next(iter(s for s in ireq.specifier), None)
                if spec:
                    version = spec.version
            for release in cleaned_releases[version]:
                collected_hashes.add(release["digests"][shims.FAVORITE_HASH])
            return self.prepend_hash_types(collected_hashes, shims.FAVORITE_HASH)
        except (ValueError, KeyError, ConnectionError):
            if self.project.s.is_verbose():
                click_echo(
                    "{}: Error generating hash for {}".format(
                        crayons.red("Warning", bold=True), ireq.name
                    ), err=True
                )
            return None

    def collect_hashes(self, ireq):
        if ireq.link:
            link = ireq.link
            if link.is_vcs or (link.is_file and link.is_existing_dir()):
                return set()
            if ireq.original_link:
                return {self._get_hash_from_link(ireq.original_link)}

        if not is_pinned_requirement(ireq):
            return set()

        if any(
            "python.org" in source["url"] or "pypi.org" in source["url"]
            for source in self.sources
        ):
            hashes = self._get_hashes_from_pypi(ireq)
            if hashes:
                return hashes

        applicable_candidates = self.finder.find_best_candidate(
            ireq.name, ireq.specifier
        ).iter_applicable()
        return {
            self._get_hash_from_link(candidate.link)
            for candidate in applicable_candidates
        }

    def resolve_hashes(self):
        if self.results is not None:
            for ireq in self.results:
                self.hashes[ireq] = self.collect_hashes(ireq)
        return self.hashes

    def _get_hash_from_link(self, link):
        from pipenv.vendor.pip_shims import shims

        if link.hash and link.hash_name == shims.FAVORITE_HASH:
            return f"{link.hash_name}:{link.hash}"

        return self.hash_cache.get_hash(link)

    def _clean_skipped_result(self, req, value):
        ref = None
        if req.is_vcs:
            ref = req.commit_hash
        ireq = req.as_ireq()
        entry = value.copy()
        entry["name"] = req.name
        if entry.get("editable", False) and entry.get("version"):
            del entry["version"]
        ref = ref if ref is not None else entry.get("ref")
        if ref:
            entry["ref"] = ref
        collected_hashes = self.collect_hashes(ireq)
        if collected_hashes:
            entry["hashes"] = sorted(set(collected_hashes))
        return req.name, entry

    def clean_results(self):
        from pipenv.vendor.requirementslib.models.requirements import (
            Requirement
        )
        reqs = [(Requirement.from_ireq(ireq), ireq) for ireq in self.resolved_tree]
        results = {}
        for req, ireq in reqs:
            if (req.vcs and req.editable and not req.is_direct_url):
                continue
            elif req.normalized_name in self.skipped.keys():
                continue
            collected_hashes = self.hashes.get(ireq, set())
            req = req.add_hashes(collected_hashes)
            if collected_hashes:
                collected_hashes = sorted(collected_hashes)
            name, entry = format_requirement_for_lockfile(
                req, self.markers_lookup, self.index_lookup, collected_hashes
            )
            entry = translate_markers(entry)
            if name in results:
                results[name].update(entry)
            else:
                results[name] = entry
        for k in list(self.skipped.keys()):
            req = Requirement.from_pipfile(k, self.skipped[k])
            name, entry = self._clean_skipped_result(req, self.skipped[k])
            entry = translate_markers(entry)
            if name in results:
                results[name].update(entry)
            else:
                results[name] = entry
        results = list(results.values())
        return results


def format_requirement_for_lockfile(req, markers_lookup, index_lookup, hashes=None):
    if req.specifiers:
        version = str(req.get_version())
    else:
        version = None
    index = index_lookup.get(req.normalized_name)
    markers = markers_lookup.get(req.normalized_name)
    req.index = index
    name, pf_entry = req.pipfile_entry
    name = pep423_name(req.name)
    entry = {}
    if isinstance(pf_entry, str):
        entry["version"] = pf_entry.lstrip("=")
    else:
        entry.update(pf_entry)
        if version is not None and not req.is_vcs:
            entry["version"] = version
        if req.line_instance.is_direct_url and not req.is_vcs:
            entry["file"] = req.req.uri
    if hashes:
        entry["hashes"] = sorted(set(hashes))
    entry["name"] = name
    if index:
        entry.update({"index": index})
    if markers:
        entry.update({"markers": markers})
    entry = translate_markers(entry)
    if req.vcs or req.editable:
        for key in ("index", "version", "file"):
            try:
                del entry[key]
            except KeyError:
                pass
    return name, entry


def _show_warning(message, category, filename, lineno, line):
    warnings.showwarning(message=message, category=category, filename=filename,
                         lineno=lineno, file=sys.stderr, line=line)
    sys.stderr.flush()


def actually_resolve_deps(
    deps,
    index_lookup,
    markers_lookup,
    project,
    sources,
    clear,
    pre,
    req_dir=None,
):
    from pipenv.vendor.vistir.path import create_tracked_tempdir

    if not req_dir:
        req_dir = create_tracked_tempdir(suffix="-requirements", prefix="pipenv-")
    warning_list = []

    with warnings.catch_warnings(record=True) as warning_list:
        resolver = Resolver.create(
            deps, project, index_lookup, markers_lookup, sources, req_dir, clear, pre
        )
        resolver.resolve()
        hashes = resolver.resolve_hashes()
        resolver.resolve_constraints()
        results = resolver.clean_results()
    for warning in warning_list:
        _show_warning(warning.message, warning.category, warning.filename, warning.lineno,
                      warning.line)
    return (results, hashes, resolver.markers_lookup, resolver, resolver.skipped)


@contextlib.contextmanager
def create_spinner(text, setting, nospin=None, spinner_name=None):
    from .vendor.vistir import spin
    from .vendor.vistir.misc import fs_str
    if not spinner_name:
        spinner_name = setting.PIPENV_SPINNER
    if nospin is None:
        nospin = setting.PIPENV_NOSPIN
    with spin.create_spinner(
        spinner_name=spinner_name,
        start_text=fs_str(text),
        nospin=nospin, write_to_stdout=False
    ) as sp:
        yield sp


def resolve(cmd, sp, project):
    from ._compat import decode_output
    from .cmdparse import Script
    from .vendor.vistir.misc import echo
    c = subprocess_run(Script.parse(cmd).cmd_args, block=False, env=os.environ.copy())
    is_verbose = project.s.is_verbose()
    err = ""
    for line in iter(c.stderr.readline, ""):
        line = decode_output(line)
        if not line.rstrip():
            continue
        err += line
        if is_verbose:
            sp.hide_and_write(line.rstrip())

    c.wait()
    returncode = c.poll()
    out = c.stdout.read()
    if returncode != 0:
        sp.red.fail(environments.PIPENV_SPINNER_FAIL_TEXT.format(
            "Locking Failed!"
        ))
        echo(out.strip(), err=True)
        if not is_verbose:
            echo(err, err=True)
        sys.exit(returncode)
    if is_verbose:
        echo(out.strip(), err=True)
    return subprocess.CompletedProcess(c.args, returncode, out, err)


def get_locked_dep(dep, pipfile_section, prefer_pipfile=True):
    # the prefer pipfile flag is not used yet, but we are introducing
    # it now for development purposes
    # TODO: Is this implementation clear? How can it be improved?
    entry = None
    cleaner_kwargs = {
        "is_top_level": False,
        "pipfile_entry": None
    }
    if isinstance(dep, Mapping) and dep.get("name", ""):
        dep_name = pep423_name(dep["name"])
        name = next(iter(
            k for k in pipfile_section.keys()
            if pep423_name(k) == dep_name
        ), None)
        entry = pipfile_section[name] if name else None

    if entry:
        cleaner_kwargs.update({"is_top_level": True, "pipfile_entry": entry})
    lockfile_entry = clean_resolved_dep(dep, **cleaner_kwargs)
    if entry and isinstance(entry, Mapping):
        version = entry.get("version", "") if entry else ""
    else:
        version = entry if entry else ""
    lockfile_name, lockfile_dict = lockfile_entry.copy().popitem()
    lockfile_version = lockfile_dict.get("version", "")
    # Keep pins from the lockfile
    if prefer_pipfile and lockfile_version != version and version.startswith("==") and "*" not in version:
        lockfile_dict["version"] = version
    lockfile_entry[lockfile_name] = lockfile_dict
    return lockfile_entry


def prepare_lockfile(results, pipfile, lockfile):
    # from .vendor.requirementslib.utils import is_vcs
    for dep in results:
        if not dep:
            continue
        # Merge in any relevant information from the pipfile entry, including
        # markers, normalized names, URL info, etc that we may have dropped during lock
        # if not is_vcs(dep):
        lockfile_entry = get_locked_dep(dep, pipfile)
        name = next(iter(k for k in lockfile_entry.keys()))
        current_entry = lockfile.get(name)
        if current_entry:
            if not isinstance(current_entry, Mapping):
                lockfile[name] = lockfile_entry[name]
            else:
                lockfile[name].update(lockfile_entry[name])
                lockfile[name] = translate_markers(lockfile[name])
        else:
            lockfile[name] = lockfile_entry[name]
    return lockfile


def venv_resolve_deps(
    deps,
    which,
    project,
    pre=False,
    clear=False,
    allow_global=False,
    pypi_mirror=None,
    dev=False,
    pipfile=None,
    lockfile=None,
    keep_outdated=False
):
    """
    Resolve dependencies for a pipenv project, acts as a portal to the target environment.

    Regardless of whether a virtual environment is present or not, this will spawn
    a subproces which is isolated to the target environment and which will perform
    dependency resolution.  This function reads the output of that call and mutates
    the provided lockfile accordingly, returning nothing.

    :param List[:class:`~requirementslib.Requirement`] deps: A list of dependencies to resolve.
    :param Callable which: [description]
    :param project: The pipenv Project instance to use during resolution
    :param Optional[bool] pre: Whether to resolve pre-release candidates, defaults to False
    :param Optional[bool] clear: Whether to clear the cache during resolution, defaults to False
    :param Optional[bool] allow_global: Whether to use *sys.executable* as the python binary, defaults to False
    :param Optional[str] pypi_mirror: A URL to substitute any time *pypi.org* is encountered, defaults to None
    :param Optional[bool] dev: Whether to target *dev-packages* or not, defaults to False
    :param pipfile: A Pipfile section to operate on, defaults to None
    :type pipfile: Optional[Dict[str, Union[str, Dict[str, bool, List[str]]]]]
    :param Dict[str, Any] lockfile: A project lockfile to mutate, defaults to None
    :param bool keep_outdated: Whether to retain outdated dependencies and resolve with them in mind, defaults to False
    :raises RuntimeError: Raised on resolution failure
    :return: Nothing
    :rtype: None
    """

    import json

    from . import resolver
    from ._compat import decode_for_output
    from .vendor.vistir.compat import JSONDecodeError, NamedTemporaryFile, Path
    from .vendor.vistir.misc import fs_str
    from .vendor.vistir.path import create_tracked_tempdir

    results = []
    pipfile_section = "dev-packages" if dev else "packages"
    lockfile_section = "develop" if dev else "default"
    if not deps:
        if not project.pipfile_exists:
            return None
        deps = project.parsed_pipfile.get(pipfile_section, {})
    if not deps:
        return None

    if not pipfile:
        pipfile = getattr(project, pipfile_section, {})
    if not lockfile:
        lockfile = project._lockfile
    req_dir = create_tracked_tempdir(prefix="pipenv", suffix="requirements")
    cmd = [
        which("python", allow_global=allow_global),
        Path(resolver.__file__.rstrip("co")).as_posix()
    ]
    if pre:
        cmd.append("--pre")
    if clear:
        cmd.append("--clear")
    if allow_global:
        cmd.append("--system")
    if dev:
        cmd.append("--dev")
    target_file = NamedTemporaryFile(prefix="resolver", suffix=".json", delete=False)
    target_file.close()
    cmd.extend(["--write", make_posix(target_file.name)])
    with temp_environ():
        os.environ.update({fs_str(k): fs_str(val) for k, val in os.environ.items()})
        if pypi_mirror:
            os.environ["PIPENV_PYPI_MIRROR"] = str(pypi_mirror)
        os.environ["PIPENV_VERBOSITY"] = str(project.s.PIPENV_VERBOSITY)
        os.environ["PIPENV_REQ_DIR"] = fs_str(req_dir)
        os.environ["PIP_NO_INPUT"] = fs_str("1")
        pipenv_site_dir = get_pipenv_sitedir()
        if pipenv_site_dir is not None:
            os.environ["PIPENV_SITE_DIR"] = pipenv_site_dir
        else:
            os.environ.pop("PIPENV_SITE_DIR", None)
        if keep_outdated:
            os.environ["PIPENV_KEEP_OUTDATED"] = fs_str("1")
        with create_spinner(text=decode_for_output("Locking..."), setting=project.s) as sp:
            # This conversion is somewhat slow on local and file-type requirements since
            # we now download those requirements / make temporary folders to perform
            # dependency resolution on them, so we are including this step inside the
            # spinner context manager for the UX improvement
            sp.write(decode_for_output("Building requirements..."))
            deps = convert_deps_to_pip(
                deps, project, r=False, include_index=True
            )
            constraints = set(deps)
            os.environ["PIPENV_PACKAGES"] = str("\n".join(constraints))
            sp.write(decode_for_output("Resolving dependencies..."))
            c = resolve(cmd, sp, project=project)
            results = c.stdout.strip()
            if c.returncode == 0:
                sp.green.ok(environments.PIPENV_SPINNER_OK_TEXT.format("Success!"))
                if not project.s.is_verbose() and c.stderr.strip():
                    click_echo(crayons.yellow(f"Warning: {c.stderr.strip()}"), err=True)
            else:
                sp.red.fail(environments.PIPENV_SPINNER_FAIL_TEXT.format("Locking Failed!"))
                click_echo(f"Output: {c.stdout.strip()}", err=True)
                click_echo(f"Error: {c.stderr.strip()}", err=True)
    try:
        with open(target_file.name) as fh:
            results = json.load(fh)
    except (IndexError, JSONDecodeError):
        click_echo(c.stdout.strip(), err=True)
        click_echo(c.stderr.strip(), err=True)
        if os.path.exists(target_file.name):
            os.unlink(target_file.name)
        raise RuntimeError("There was a problem with locking.")
    if os.path.exists(target_file.name):
        os.unlink(target_file.name)
    if lockfile_section not in lockfile:
        lockfile[lockfile_section] = {}
    prepare_lockfile(results, pipfile, lockfile[lockfile_section])


def resolve_deps(
    deps,
    which,
    project,
    sources=None,
    python=False,
    clear=False,
    pre=False,
    allow_global=False,
    req_dir=None
):
    """Given a list of dependencies, return a resolved list of dependencies,
    using pip-tools -- and their hashes, using the warehouse API / pip.
    """
    index_lookup = {}
    markers_lookup = {}
    python_path = which("python", allow_global=allow_global)
    if not os.environ.get("PIP_SRC"):
        os.environ["PIP_SRC"] = project.virtualenv_src_location
    backup_python_path = sys.executable
    results = []
    resolver = None
    if not deps:
        return results, resolver
    # First (proper) attempt:
    req_dir = req_dir if req_dir else os.environ.get("req_dir", None)
    if not req_dir:
        from .vendor.vistir.path import create_tracked_tempdir
        req_dir = create_tracked_tempdir(prefix="pipenv-", suffix="-requirements")
    with HackedPythonVersion(python_version=python, python_path=python_path):
        try:
            results, hashes, markers_lookup, resolver, skipped = actually_resolve_deps(
                deps,
                index_lookup,
                markers_lookup,
                project,
                sources,
                clear,
                pre,
                req_dir=req_dir,
            )
        except RuntimeError:
            # Don't exit here, like usual.
            results = None
    # Second (last-resort) attempt:
    if results is None:
        with HackedPythonVersion(
            python_version=".".join([str(s) for s in sys.version_info[:3]]),
            python_path=backup_python_path,
        ):
            try:
                # Attempt to resolve again, with different Python version information,
                # particularly for particularly particular packages.
                results, hashes, markers_lookup, resolver, skipped = actually_resolve_deps(
                    deps,
                    index_lookup,
                    markers_lookup,
                    project,
                    sources,
                    clear,
                    pre,
                    req_dir=req_dir,
                )
            except RuntimeError:
                sys.exit(1)
    return results, resolver


def is_star(val):
    return isinstance(val, str) and val == "*"


def is_pinned(val):
    if isinstance(val, Mapping):
        val = val.get("version")
    return isinstance(val, str) and val.startswith("==")


def is_pinned_requirement(ireq):
    """
    Returns whether an InstallRequirement is a "pinned" requirement.
    """
    if ireq.editable:
        return False

    if ireq.req is None or len(ireq.specifier) != 1:
        return False

    spec = next(iter(ireq.specifier))
    return spec.operator in {"==", "==="} and not spec.version.endswith(".*")


def convert_deps_to_pip(deps, project=None, r=True, include_index=True):
    """"Converts a Pipfile-formatted dependency to a pip-formatted one."""
    from .vendor.requirementslib.models.requirements import Requirement

    dependencies = []
    for dep_name, dep in deps.items():
        if project:
            project.clear_pipfile_cache()
        indexes = getattr(project, "pipfile_sources", []) if project is not None else []
        new_dep = Requirement.from_pipfile(dep_name, dep)
        if new_dep.index:
            include_index = True
        req = new_dep.as_line(sources=indexes if include_index else None).strip()
        dependencies.append(req)
    if not r:
        return dependencies

    # Write requirements.txt to tmp directory.
    from .vendor.vistir.path import create_tracked_tempfile
    f = create_tracked_tempfile(suffix="-requirements.txt", delete=False)
    f.write("\n".join(dependencies).encode("utf-8"))
    f.close()
    return f.name


def mkdir_p(newdir):
    """works the way a good mkdir should :)
        - already exists, silently complete
        - regular file in the way, raise an exception
        - parent directory(ies) does not exist, make them as well
        From: http://code.activestate.com/recipes/82465-a-friendly-mkdir/
    """
    if os.path.isdir(newdir):
        pass
    elif os.path.isfile(newdir):
        raise OSError(
            "a file with the same name as the desired dir, '{}', already exists.".format(
                newdir
            )
        )

    else:
        head, tail = os.path.split(newdir)
        if head and not os.path.isdir(head):
            mkdir_p(head)
        if tail:
            # Even though we've checked that the directory doesn't exist above, it might exist
            # now if some other process has created it between now and the time we checked it.
            try:
                os.mkdir(newdir)
            except OSError as exn:
                # If we failed because the directory does exist, that's not a problem -
                # that's what we were trying to do anyway. Only re-raise the exception
                # if we failed for some other reason.
                if exn.errno != errno.EEXIST:
                    raise


def is_required_version(version, specified_version):
    """Check to see if there's a hard requirement for version
    number provided in the Pipfile.
    """
    # Certain packages may be defined with multiple values.
    if isinstance(specified_version, dict):
        specified_version = specified_version.get("version", "")
    if specified_version.startswith("=="):
        return version.strip() == specified_version.split("==")[1].strip()

    return True


def is_editable(pipfile_entry):
    if hasattr(pipfile_entry, "get"):
        return pipfile_entry.get("editable", False) and any(
            pipfile_entry.get(key) for key in ("file", "path") + VCS_LIST
        )
    return False


def is_installable_file(path):
    """Determine if a path can potentially be installed"""
    from .patched.notpip._internal.utils.packaging import specifiers
    from .vendor.pip_shims.shims import is_archive_file, is_installable_dir

    if hasattr(path, "keys") and any(
        key for key in path.keys() if key in ["file", "path"]
    ):
        path = urlparse(path["file"]).path if "file" in path else path["path"]
    if not isinstance(path, str) or path == "*":
        return False

    # If the string starts with a valid specifier operator, test if it is a valid
    # specifier set before making a path object (to avoid breaking windows)
    if any(path.startswith(spec) for spec in "!=<>~"):
        try:
            specifiers.SpecifierSet(path)
        # If this is not a valid specifier, just move on and try it as a path
        except specifiers.InvalidSpecifier:
            pass
        else:
            return False

    if not os.path.exists(os.path.abspath(path)):
        return False

    lookup_path = Path(path)
    absolute_path = f"{lookup_path.absolute()}"
    if lookup_path.is_dir() and is_installable_dir(absolute_path):
        return True

    elif lookup_path.is_file() and is_archive_file(absolute_path):
        return True

    return False


def is_file(package):
    """Determine if a package name is for a File dependency."""
    if hasattr(package, "keys"):
        return any(key for key in package.keys() if key in ["file", "path"])

    if os.path.exists(str(package)):
        return True

    for start in SCHEME_LIST:
        if str(package).startswith(start):
            return True

    return False


def pep440_version(version):
    """Normalize version to PEP 440 standards"""
    # Use pip built-in version parser.
    from pipenv.vendor.pip_shims import shims

    return str(shims.parse_version(version))


def pep423_name(name):
    """Normalize package name to PEP 423 style standard."""
    name = name.lower()
    if any(i not in name for i in (VCS_LIST + SCHEME_LIST)):
        return name.replace("_", "-")

    else:
        return name


def proper_case(package_name):
    """Properly case project name from pypi.org."""
    # Hit the simple API.
    r = _get_requests_session().get(
        f"https://pypi.org/pypi/{package_name}/json", timeout=0.3, stream=True
    )
    if not r.ok:
        raise OSError(
            f"Unable to find package {package_name} in PyPI repository."
        )

    r = parse.parse("https://pypi.org/pypi/{name}/json", r.url)
    good_name = r["name"]
    return good_name


def get_windows_path(*args):
    """Sanitize a path for windows environments

    Accepts an arbitrary list of arguments and makes a clean windows path"""
    return os.path.normpath(os.path.join(*args))


def find_windows_executable(bin_path, exe_name):
    """Given an executable name, search the given location for an executable"""
    requested_path = get_windows_path(bin_path, exe_name)
    if os.path.isfile(requested_path):
        return requested_path

    try:
        pathext = os.environ["PATHEXT"]
    except KeyError:
        pass
    else:
        for ext in pathext.split(os.pathsep):
            path = get_windows_path(bin_path, exe_name + ext.strip().lower())
            if os.path.isfile(path):
                return path

    return find_executable(exe_name)


def path_to_url(path):

    return Path(normalize_drive(os.path.abspath(path))).as_uri()


def normalize_path(path):
    return os.path.expandvars(os.path.expanduser(
        os.path.normcase(os.path.normpath(os.path.abspath(str(path))))
    ))


def get_url_name(url):
    if not isinstance(url, str):
        return
    return urllib3_util.parse_url(url).host


def get_canonical_names(packages):
    """Canonicalize a list of packages and return a set of canonical names"""
    from .vendor.packaging.utils import canonicalize_name

    if not isinstance(packages, Sequence):
        if not isinstance(packages, str):
            return packages
        packages = [packages]
    return {canonicalize_name(pkg) for pkg in packages if pkg}


def walk_up(bottom):
    """Mimic os.walk, but walk 'up' instead of down the directory tree.
    From: https://gist.github.com/zdavkeos/1098474
    """
    bottom = os.path.realpath(bottom)
    # Get files in current dir.
    try:
        names = os.listdir(bottom)
    except Exception:
        return

    dirs, nondirs = [], []
    for name in names:
        if os.path.isdir(os.path.join(bottom, name)):
            dirs.append(name)
        else:
            nondirs.append(name)
    yield bottom, dirs, nondirs

    new_path = os.path.realpath(os.path.join(bottom, ".."))
    # See if we are at the top.
    if new_path == bottom:
        return

    yield from walk_up(new_path)


def find_requirements(max_depth=3):
    """Returns the path of a requirements.txt file in parent directories."""
    i = 0
    for c, d, f in walk_up(os.getcwd()):
        i += 1
        if i < max_depth:
            r = os.path.join(c, "requirements.txt")
            if os.path.isfile(r):
                return r

    raise RuntimeError("No requirements.txt found!")


# Borrowed from Pew.
# See https://github.com/berdario/pew/blob/master/pew/_utils.py#L82
@contextmanager
def temp_environ():
    """Allow the ability to set os.environ temporarily"""
    environ = dict(os.environ)
    try:
        yield

    finally:
        os.environ.clear()
        os.environ.update(environ)


@contextmanager
def temp_path():
    """Allow the ability to set os.environ temporarily"""
    path = [p for p in sys.path]
    try:
        yield
    finally:
        sys.path = [p for p in path]


def load_path(python):
    import json

    from pathlib import Path
    python = Path(python).as_posix()
    json_dump_commmand = '"import json, sys; print(json.dumps(sys.path));"'
    c = subprocess_run([python, "-c", json_dump_commmand])
    if c.returncode == 0:
        return json.loads(c.stdout.strip())
    else:
        return []


def is_valid_url(url):
    """Checks if a given string is an url"""
    pieces = urlparse(url)
    return all([pieces.scheme, pieces.netloc])


def is_pypi_url(url):
    return bool(re.match(r"^http[s]?:\/\/pypi(?:\.python)?\.org\/simple[\/]?$", url))


def replace_pypi_sources(sources, pypi_replacement_source):
    return [pypi_replacement_source] + [
        source for source in sources if not is_pypi_url(source["url"])
    ]


def create_mirror_source(url):
    return {
        "url": url,
        "verify_ssl": url.startswith("https://"),
        "name": urlparse(url).hostname,
    }


def download_file(url, filename, max_retries=1):
    """Downloads file from url to a path with filename"""
    r = _get_requests_session(max_retries).get(url, stream=True)
    if not r.ok:
        raise OSError("Unable to download file")

    with open(filename, "wb") as f:
        f.write(r.content)


def normalize_drive(path):
    """Normalize drive in path so they stay consistent.

    This currently only affects local drives on Windows, which can be
    identified with either upper or lower cased drive names. The case is
    always converted to uppercase because it seems to be preferred.

    See: <https://github.com/pypa/pipenv/issues/1218>
    """
    if os.name != "nt" or not isinstance(path, str):
        return path

    drive, tail = os.path.splitdrive(path)
    # Only match (lower cased) local drives (e.g. 'c:'), not UNC mounts.
    if drive.islower() and len(drive) == 2 and drive[1] == ":":
        return f"{drive.upper()}{tail}"

    return path


def is_readonly_path(fn):
    """Check if a provided path exists and is readonly.

    Permissions check is `bool(path.stat & stat.S_IREAD)` or `not os.access(path, os.W_OK)`
    """
    if os.path.exists(fn):
        return (os.stat(fn).st_mode & stat.S_IREAD) or not os.access(fn, os.W_OK)

    return False


def set_write_bit(fn):
    if isinstance(fn, str) and not os.path.exists(fn):
        return
    os.chmod(fn, stat.S_IWRITE | stat.S_IWUSR | stat.S_IRUSR)
    return


def rmtree(directory, ignore_errors=False):
    shutil.rmtree(
        directory, ignore_errors=ignore_errors, onerror=handle_remove_readonly
    )


def handle_remove_readonly(func, path, exc):
    """Error handler for shutil.rmtree.

    Windows source repo folders are read-only by default, so this error handler
    attempts to set them as writeable and then proceed with deletion."""
    # Check for read-only attribute
    default_warning_message = (
        "Unable to remove file due to permissions restriction: {!r}"
    )
    # split the initial exception out into its type, exception, and traceback
    exc_type, exc_exception, exc_tb = exc
    if is_readonly_path(path):
        # Apply write permission and call original function
        set_write_bit(path)
        try:
            func(path)
        except OSError as e:
            if e.errno in [errno.EACCES, errno.EPERM]:
                warnings.warn(default_warning_message.format(path), ResourceWarning)
                return

    if exc_exception.errno in [errno.EACCES, errno.EPERM]:
        warnings.warn(default_warning_message.format(path), ResourceWarning)
        return

    raise exc


def escape_cmd(cmd):
    if any(special_char in cmd for special_char in ["<", ">", "&", ".", "^", "|", "?"]):
        cmd = f'\"{cmd}\"'
    return cmd


def safe_expandvars(value):
    """Call os.path.expandvars if value is a string, otherwise do nothing.
    """
    if isinstance(value, str):
        return os.path.expandvars(value)
    return value


def get_vcs_deps(
    project=None,
    dev=False,
    pypi_mirror=None,
    packages=None,
    reqs=None
):
    from .vendor.requirementslib.models.requirements import Requirement

    section = "vcs_dev_packages" if dev else "vcs_packages"
    if reqs is None:
        reqs = []
    lockfile = {}
    if not reqs:
        if not project and not packages:
            raise ValueError(
                "Must supply either a project or a pipfile section to lock vcs dependencies."
            )
        if not packages:
            try:
                packages = getattr(project, section)
            except AttributeError:
                return [], []
        reqs = [Requirement.from_pipfile(name, entry) for name, entry in packages.items()]
    result = []
    for requirement in reqs:
        name = requirement.normalized_name
        commit_hash = None
        if requirement.is_vcs:
            try:
                with temp_path(), locked_repository(requirement) as repo:
                    from pipenv.vendor.requirementslib.models.requirements import (
                        Requirement
                    )

                    # from distutils.sysconfig import get_python_lib
                    # sys.path = [repo.checkout_directory, "", ".", get_python_lib(plat_specific=0)]
                    commit_hash = repo.get_commit_hash()
                    name = requirement.normalized_name
                    lockfile[name] = requirement.pipfile_entry[1]
                    lockfile[name]['ref'] = commit_hash
                    result.append(requirement)
            except OSError:
                continue
    return result, lockfile


def translate_markers(pipfile_entry):
    """Take a pipfile entry and normalize its markers

    Provide a pipfile entry which may have 'markers' as a key or it may have
    any valid key from `packaging.markers.marker_context.keys()` and standardize
    the format into {'markers': 'key == "some_value"'}.

    :param pipfile_entry: A dictionariy of keys and values representing a pipfile entry
    :type pipfile_entry: dict
    :returns: A normalized dictionary with cleaned marker entries
    """
    if not isinstance(pipfile_entry, Mapping):
        raise TypeError("Entry is not a pipfile formatted mapping.")
    from .vendor.packaging.markers import default_environment
    from .vendor.vistir.misc import dedup

    allowed_marker_keys = ["markers"] + list(default_environment().keys())
    provided_keys = list(pipfile_entry.keys()) if hasattr(pipfile_entry, "keys") else []
    pipfile_markers = set(provided_keys) & set(allowed_marker_keys)
    new_pipfile = dict(pipfile_entry).copy()
    marker_set = set()
    if "markers" in new_pipfile:
        marker_str = new_pipfile.pop("markers")
        if marker_str:
            marker = str(Marker(marker_str))
            if 'extra' not in marker:
                marker_set.add(marker)
    for m in pipfile_markers:
        entry = f"{pipfile_entry[m]}"
        if m != "markers":
            marker_set.add(str(Marker(f"{m} {entry}")))
            new_pipfile.pop(m)
    if marker_set:
        new_pipfile["markers"] = str(Marker(" or ".join(
            f"{s}" if " and " in s else s
            for s in sorted(dedup(marker_set))
        ))).replace('"', "'")
    return new_pipfile


def clean_resolved_dep(dep, is_top_level=False, pipfile_entry=None):
    from .vendor.requirementslib.utils import is_vcs
    name = pep423_name(dep["name"])
    lockfile = {}
    # We use this to determine if there are any markers on top level packages
    # So we can make sure those win out during resolution if the packages reoccur
    if "version" in dep and dep["version"] and not dep.get("editable", False):
        version = "{}".format(dep["version"])
        if not version.startswith("=="):
            version = f"=={version}"
        lockfile["version"] = version
    if is_vcs(dep):
        ref = dep.get("ref", None)
        if ref is not None:
            lockfile["ref"] = ref
        vcs_type = next(iter(k for k in dep.keys() if k in VCS_LIST), None)
        if vcs_type:
            lockfile[vcs_type] = dep[vcs_type]
        if "subdirectory" in dep:
            lockfile["subdirectory"] = dep["subdirectory"]
    for key in ["hashes", "index", "extras", "editable"]:
        if key in dep:
            lockfile[key] = dep[key]
    # In case we lock a uri or a file when the user supplied a path
    # remove the uri or file keys from the entry and keep the path
    fs_key = next(iter(k for k in ["path", "file"] if k in dep), None)
    pipfile_fs_key = None
    if pipfile_entry:
        pipfile_fs_key = next(iter(k for k in ["path", "file"] if k in pipfile_entry), None)
    if fs_key and pipfile_fs_key and fs_key != pipfile_fs_key:
        lockfile[pipfile_fs_key] = pipfile_entry[pipfile_fs_key]
    elif fs_key is not None:
        lockfile[fs_key] = dep[fs_key]

    # If a package is **PRESENT** in the pipfile but has no markers, make sure we
    # **NEVER** include markers in the lockfile
    if "markers" in dep and dep.get("markers", "").strip():
        # First, handle the case where there is no top level dependency in the pipfile
        if not is_top_level:
            translated = translate_markers(dep).get("markers", "").strip()
            if translated:
                try:
                    lockfile["markers"] = translated
                except TypeError:
                    pass
        # otherwise make sure we are prioritizing whatever the pipfile says about the markers
        # If the pipfile says nothing, then we should put nothing in the lockfile
        else:
            try:
                pipfile_entry = translate_markers(pipfile_entry)
                lockfile["markers"] = pipfile_entry.get("markers")
            except TypeError:
                pass
    return {name: lockfile}


def get_workon_home():
    workon_home = os.environ.get("WORKON_HOME")
    if not workon_home:
        if os.name == "nt":
            workon_home = "~/.virtualenvs"
        else:
            workon_home = os.path.join(
                os.environ.get("XDG_DATA_HOME", "~/.local/share"), "virtualenvs"
            )
    # Create directory if it does not already exist
    expanded_path = Path(os.path.expandvars(workon_home)).expanduser()
    mkdir_p(str(expanded_path))
    return expanded_path


def is_virtual_environment(path):
    """Check if a given path is a virtual environment's root.

    This is done by checking if the directory contains a Python executable in
    its bin/Scripts directory. Not technically correct, but good enough for
    general usage.
    """
    if not path.is_dir():
        return False
    for bindir_name in ('bin', 'Scripts'):
        for python in path.joinpath(bindir_name).glob('python*'):
            try:
                exeness = python.is_file() and os.access(str(python), os.X_OK)
            except OSError:
                exeness = False
            if exeness:
                return True
    return False


@contextmanager
def locked_repository(requirement):
    from .vendor.vistir.path import create_tracked_tempdir
    if not requirement.is_vcs:
        return
    original_base = os.environ.pop("PIP_SHIMS_BASE_MODULE", None)
    os.environ["PIP_SHIMS_BASE_MODULE"] = fs_str("pipenv.patched.notpip")
    src_dir = create_tracked_tempdir(prefix="pipenv-", suffix="-src")
    try:
        with requirement.req.locked_vcs_repo(src_dir=src_dir) as repo:
            yield repo
    finally:
        if original_base:
            os.environ["PIP_SHIMS_BASE_MODULE"] = original_base


@contextmanager
def chdir(path):
    """Context manager to change working directories."""
    if not path:
        return
    prev_cwd = Path.cwd().as_posix()
    if isinstance(path, Path):
        path = path.as_posix()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(prev_cwd)


def looks_like_dir(path):
    seps = (sep for sep in (os.path.sep, os.path.altsep) if sep is not None)
    return any(sep in path for sep in seps)


def parse_indexes(line, strict=False):
    from argparse import ArgumentParser

    comment_re = re.compile(r"(?:^|\s+)#.*$")
    line = comment_re.sub("", line)
    parser = ArgumentParser("indexes")
    parser.add_argument("-i", "--index-url", dest="index")
    parser.add_argument("--extra-index-url", dest="extra_index")
    parser.add_argument("--trusted-host", dest="trusted_host")
    args, remainder = parser.parse_known_args(line.split())
    index = args.index
    extra_index = args.extra_index
    trusted_host = args.trusted_host
    if strict and sum(
        bool(arg) for arg in (index, extra_index, trusted_host, remainder)
    ) > 1:
        raise ValueError("Index arguments must be on their own lines.")
    return index, extra_index, trusted_host, remainder


@contextmanager
def sys_version(version_tuple):
    """
    Set a temporary sys.version_info tuple

    :param version_tuple: a fake sys.version_info tuple
    """

    old_version = sys.version_info
    sys.version_info = version_tuple
    yield
    sys.version_info = old_version


def add_to_set(original_set, element):
    """Given a set and some arbitrary element, add the element(s) to the set"""
    if not element:
        return original_set
    if isinstance(element, Set):
        original_set |= element
    elif isinstance(element, (list, tuple)):
        original_set |= set(element)
    else:
        original_set.add(element)
    return original_set


def is_url_equal(url, other_url):
    # type: (str, str) -> bool
    """
    Compare two urls by scheme, host, and path, ignoring auth

    :param str url: The initial URL to compare
    :param str url: Second url to compare to the first
    :return: Whether the URLs are equal without **auth**, **query**, and **fragment**
    :rtype: bool

    >>> is_url_equal("https://user:pass@mydomain.com/some/path?some_query",
                     "https://user2:pass2@mydomain.com/some/path")
    True

    >>> is_url_equal("https://user:pass@mydomain.com/some/path?some_query",
                 "https://mydomain.com/some?some_query")
    False
    """
    if not isinstance(url, str):
        raise TypeError(f"Expected string for url, received {url!r}")
    if not isinstance(other_url, str):
        raise TypeError(f"Expected string for url, received {other_url!r}")
    parsed_url = urllib3_util.parse_url(url)
    parsed_other_url = urllib3_util.parse_url(other_url)
    unparsed = parsed_url._replace(auth=None, query=None, fragment=None).url
    unparsed_other = parsed_other_url._replace(auth=None, query=None, fragment=None).url
    return unparsed == unparsed_other


@lru_cache()
def make_posix(path):
    # type: (str) -> str
    """
    Convert a path with possible windows-style separators to a posix-style path
    (with **/** separators instead of **\\** separators).

    :param Text path: A path to convert.
    :return: A converted posix-style path
    :rtype: Text

    >>> make_posix("c:/users/user/venvs/some_venv\\Lib\\site-packages")
    "c:/users/user/venvs/some_venv/Lib/site-packages"

    >>> make_posix("c:\\users\\user\\venvs\\some_venv")
    "c:/users/user/venvs/some_venv"
    """
    if not isinstance(path, str):
        raise TypeError(f"Expected a string for path, received {path!r}...")
    starts_with_sep = path.startswith(os.path.sep)
    separated = normalize_path(path).split(os.path.sep)
    if isinstance(separated, (list, tuple)):
        path = posixpath.join(*separated)
        if starts_with_sep:
            path = f"/{path}"
    return path


def get_pipenv_dist(pkg="pipenv", pipenv_site=None):
    from .resolver import find_site_path
    pipenv_libdir = os.path.dirname(os.path.abspath(__file__))
    if pipenv_site is None:
        pipenv_site = os.path.dirname(pipenv_libdir)
    pipenv_dist, _ = find_site_path(pkg, site_dir=pipenv_site)
    return pipenv_dist


def find_python(finder, line=None):
    """
    Given a `pythonfinder.Finder` instance and an optional line, find a corresponding python

    :param finder: A :class:`pythonfinder.Finder` instance to use for searching
    :type finder: :class:pythonfinder.Finder`
    :param str line: A version, path, name, or nothing, defaults to None
    :return: A path to python
    :rtype: str
    """

    if line and not isinstance(line, str):
        raise TypeError(
            f"Invalid python search type: expected string, received {line!r}"
        )
    if line and os.path.isabs(line):
        if os.name == "nt":
            line = make_posix(line)
        return line
    if not finder:
        from pipenv.vendor.pythonfinder import Finder
        finder = Finder(global_search=True)
    if not line:
        result = next(iter(finder.find_all_python_versions()), None)
    elif line and line[0].isdigit() or re.match(r'[\d\.]+', line):
        result = finder.find_python_version(line)
    else:
        result = finder.find_python_version(name=line)
    if not result:
        result = finder.which(line)
    if not result and not line.startswith("python"):
        line = f"python{line}"
        result = find_python(finder, line)

    if result:
        if not isinstance(result, str):
            return result.path.as_posix()
        return result
    return


def is_python_command(line):
    """
    Given an input, checks whether the input is a request for python or notself.

    This can be a version, a python runtime name, or a generic 'python' or 'pythonX.Y'

    :param str line: A potential request to find python
    :returns: Whether the line is a python lookup
    :rtype: bool
    """

    if not isinstance(line, str):
        raise TypeError(f"Not a valid command to check: {line!r}")

    from pipenv.vendor.pythonfinder.utils import PYTHON_IMPLEMENTATIONS
    is_version = re.match(r'\d+(\.\d+)*', line)
    if (line.startswith("python") or is_version
            or any(line.startswith(v) for v in PYTHON_IMPLEMENTATIONS)):
        return True
    # we are less sure about this but we can guess
    if line.startswith("py"):
        return True
    return False


@contextlib.contextmanager
def interrupt_handled_subprocess(
    cmd, verbose=False, return_object=True, write_to_stdout=False, combine_stderr=True,
    block=True, nospin=True, env=None
):
    """Given a :class:`subprocess.Popen` instance, wrap it in exception handlers.

    Terminates the subprocess when and if a `SystemExit` or `KeyboardInterrupt` are
    processed.

    Arguments:
        :param str cmd: A command to run
        :param bool verbose: Whether to run with verbose mode enabled, default False
        :param bool return_object: Whether to return a subprocess instance or a 2-tuple, default True
        :param bool write_to_stdout: Whether to write directly to stdout, default False
        :param bool combine_stderr: Whether to combine stdout and stderr, default True
        :param bool block: Whether the subprocess should be a blocking subprocess, default True
        :param bool nospin: Whether to suppress the spinner with the subprocess, default True
        :param Optional[Dict[str, str]] env: A dictionary to merge into the subprocess environment
        :return: A subprocess, wrapped in exception handlers, as a context manager
        :rtype: :class:`subprocess.Popen` obj: An instance of a running subprocess
    """
    obj = run(
        cmd, verbose=verbose, return_object=True, write_to_stdout=False,
        combine_stderr=False, block=True, nospin=True, env=env,
    )
    try:
        yield obj
    except (SystemExit, KeyboardInterrupt):
        if os.name == "nt":
            os.kill(obj.pid, signal.CTRL_BREAK_EVENT)
        else:
            os.kill(obj.pid, signal.SIGINT)
        obj.wait()
        raise


def subprocess_run(
    args, *, block=True, text=True, capture_output=True,
    encoding="utf-8", env=None, **other_kwargs
):
    """A backward compatible version of subprocess.run().

    It outputs text with default encoding, and store all outputs in the returned object instead of
    printing onto stdout.
    """
    _env = os.environ.copy()
    _env["PYTHONIOENCODING"] = encoding
    if env:
        _env.update(env)
    other_kwargs["env"] = _env
    if capture_output:
        other_kwargs['stdout'] = subprocess.PIPE
        other_kwargs['stderr'] = subprocess.PIPE
    if block:
        return subprocess.run(
            args, universal_newlines=text,
            encoding=encoding, **other_kwargs
        )
    else:
        return subprocess.Popen(
            args, universal_newlines=text,
            encoding=encoding, **other_kwargs
        )


def cmd_list_to_shell(args):
    """Convert a list of arguments to a quoted shell command."""
    return " ".join(shlex.quote(str(token)) for token in args)
