# Licensed under a 3-clause BSD style license - see LICENSE.rst
# ONLY works on newer python versions
import os
import re
from pathlib import Path

import yaml
from yaml import load

try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader

import libmambapy
from importlib_metadata import version as get_version

from .. import environment, util
from ..console import log

if int(get_version('libmambapy').split(".")[0]) >= 2:
    raise environment.EnvironmentUnavailable(
        f"libmambapy must be less than 2.0, but got {get_version('libmambapy')}"
    )


from ._mamba_helpers import MambaSolver

# Like Conda, Mamba also needs to be serialized
util.new_multiprocessing_lock("mamba_lock")


def _mamba_lock():
    # function; for easier monkeypatching
    return util.get_multiprocessing_lock("mamba_lock")


class Mamba(environment.Environment):
    """
    Manage an environment using mamba.

    Dependencies are installed using ``mamba``.  The benchmarked
    project is installed using ``pip``.
    """

    tool_name = "mamba"
    _matches_cache = {}

    def __init__(self, conf, python, requirements, tagged_env_vars):
        """
        Parameters
        ----------
        conf : Config instance

        python : str
            Version of Python.  Must be of the form "MAJOR.MINOR".

        requirements : dict
            Dictionary mapping a PyPI package name to a version
            identifier string.
        """
        self._python = python
        self._requirements = requirements
        self._mamba_channels = conf.conda_channels
        self._mamba_environment_file = None

        if conf.conda_environment_file == "IGNORE":
            log.debug("Skipping environment file due to conda_environment_file set to IGNORE")
            self._mamba_environment_file = None
        elif not conf.conda_environment_file:
            if (Path("environment.yml")).exists():
                log.debug("Using environment.yml")
                self._mamba_environment_file = "environment.yml"
        else:
            if (Path(conf.conda_environment_file)).exists():
                log.debug(f"Using {conf.conda_environment_file}")
                self._mamba_environment_file = conf.conda_environment_file
            else:
                log.debug(f"Environment file {conf.conda_environment_file} not found, ignoring")

        super(Mamba, self).__init__(conf, python, requirements, tagged_env_vars)
        self.context = libmambapy.Context()
        self.context.pkgs_dirs = [f"{self._env_dir}/pkgs"]
        # Handle MAMBARC environment variable
        mambarc_path = Path(os.getenv("MAMBARC", ""))
        if mambarc_path.is_file():
            with mambarc_path.open() as f:
                condarc_data = yaml.safe_load(f)
                self._apply_condarc_settings(condarc_data)

    def _apply_condarc_settings(self, condarc_data):
        # Apply channel settings
        if 'channels' in condarc_data:
            self.context.channels = condarc_data['channels']
            self._mamba_channels.extend(condarc_data['channels'])

        # Apply channel priority settings
        channel_priority_map = {
            'strict': libmambapy.ChannelPriority.kStrict,
            'flexible': libmambapy.ChannelPriority.kFlexible,
            'disabled': libmambapy.ChannelPriority.kDisabled
        }
        if 'channel_priority' in condarc_data:
            priority_str = condarc_data['channel_priority']
            if priority_str in channel_priority_map:
                self.context.channel_priority = channel_priority_map[priority_str]
            else:
                log.debug(f"Unknown channel priority: {priority_str}")

    @classmethod
    def matches(cls, python):
        # Calling mamba can take a long time, so remember the result
        if python not in cls._matches_cache:
            cls._matches_cache[python] = cls._matches(python)
        return cls._matches_cache[python]

    @classmethod
    def _matches(cls, python):
        if not re.match(r'^[0-9].*$', python):
            return False
        else:
            if os.getenv("CONDA_EXE"):
                mamba_path = str(Path(os.getenv("CONDA_EXE")).parent / "mamba")
            else:
                return False
            try:
                return util.search_channels(mamba_path, "python", python)
            except util.ProcessError:
                return False

    def _setup(self):
        log.info(f"Creating mamba environment for {self.name}")

        mamba_args, pip_args = self._get_requirements()
        if len(pip_args) > 0:
            self.context.add_pip_as_python_dependency = True
        env = dict(os.environ)
        env.update(self.build_env_vars)
        Path(f"{self._path}/conda-meta").mkdir(parents=True, exist_ok=True)
        if not self._mamba_environment_file:
            # Construct payload
            mamba_pkgs = ["wheel", "pip"]
        else:
            # For named environments
            env_file_name = self._mamba_environment_file
            env_data = load(Path(env_file_name).open(), Loader=Loader)
            mamba_pkgs = [x for x in env_data.get("dependencies", []) if isinstance(x, str)]
            self._mamba_channels += [x for x in env_data.get("channels", []) if isinstance(x, str)]
            self._mamba_channels = list(dict.fromkeys(self._mamba_channels).keys())
            # Handle possible pip keys
            pip_maybe = [x for x in env_data.get("dependencies", []) if isinstance(x, dict)]
            if len(pip_maybe) == 1:
                try:
                    pip_args += pip_maybe[0]["pip"]
                except KeyError:
                    raise KeyError("Only pip is supported as a secondary key")
        mamba_pkgs += mamba_args
        # Changed in v0.6.5, gh-1294
        # previously, the user provided environment was assumed to handle the python version
        mamba_pkgs = [
            util.replace_python_version(pkg, self._python) for pkg in mamba_pkgs
        ]
        self.context.prefix_params.target_prefix = self._path
        solver = MambaSolver(
            self._mamba_channels, None, self.context  # or target_platform
        )
        with _mamba_lock():
            transaction = solver.solve(mamba_pkgs)
            transaction.execute(libmambapy.PrefixData(self._path))
            if pip_args:
                for declaration in pip_args:
                    parsed_declaration = util.ParsedPipDeclaration(declaration)
                    pip_call = util.construct_pip_call(self._run_pip, parsed_declaration)
                    pip_call()

    def _get_requirements(self):
        mamba_args = []
        pip_args = []

        for key, val in {**self._requirements,
                         **self._base_requirements}.items():
            if key.startswith("pip+"):
                pip_args.append(f"{key[4:]} {val}")
            else:
                if val:
                    mamba_args.append(f"{key}={val}")
                else:
                    mamba_args.append(key)

        return mamba_args, pip_args

    def run_executable(self, executable, args, **kwargs):
        return super(Mamba, self).run_executable(executable, args, **kwargs)

    def run(self, args, **kwargs):
        log.debug(f"Running '{' '.join(args)}' in {self.name}")
        return self.run_executable("python", args, **kwargs)

    def _run_pip(self, args, **kwargs):
        # Run pip via python -m pip, so that it works on Windows when
        # upgrading pip itself, and avoids shebang length limit on Linux
        return self.run_executable("python", ["-m", "pip"] + list(args), **kwargs)
