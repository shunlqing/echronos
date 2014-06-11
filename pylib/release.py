import io
import os
import shutil
import logging
import inspect
import tarfile
import subprocess
from glob import glob
from contextlib import contextmanager
from .utils import chdir, tempdir, get_host_platform_name, BASE_TIME, top_path, base_to_top_paths, Git
from .components import build


class _ReleaseMeta(type):
    """A pretty-printing meta-class for the Release class."""
    def __str__(cls):
        return '{}-{}'.format(cls.release_name, cls.version)


class Release(metaclass=_ReleaseMeta):
    """The Release base class is used by the release configuration."""
    packages = []
    platforms = []
    version = None
    product_name = None
    release_name = None
    enabled = False
    license = None
    top_level_license = None
    extra_files = []


class Package:
    """Represents a customer visible package.

    This is currently used mainly for release management.

    """
    @staticmethod
    def create_from_disk(topdir):
        """Return a dictionary that contains a Package instance for each package on disk in a 'package' directory.

        The dictionary keys are the package names.
        The dictionary values are the package instances.

        """
        pkgs = {}
        for pkg_parent_dir in base_to_top_paths(topdir, 'packages'):
            pkg_names = os.listdir(pkg_parent_dir)
            for pkg_name in pkg_names:
                pkg_path = os.path.join(pkg_parent_dir, pkg_name)
                if pkg_name in pkgs:
                    logging.warn('Overriding package {} with package {}'.format(pkgs[pkg_name].path, pkg_path))
                pkgs[pkg_name] = Package(pkg_path)
        return pkgs

    def __init__(self, path):
        assert os.path.isdir(path)
        self.path = path
        self.name = os.path.basename(self.path)


class _ReleasePackage:
    """Represents a Package instance that is refined for a specific release configuration.

    Configuring a Package instance for release results in additional properties of a package, relevant when creating
    release files.

    """
    def __init__(self, package, release_configuration):
        self._pkg = package
        self._rls_cfg = release_configuration

    def get_name(self):
        return self._pkg.name

    def get_path(self):
        return self._pkg.path

    def get_archive_name(self):
        return '{}-{}'.format(self._pkg.name, self._rls_cfg.release_name)

    def get_path_in_archive(self):
        return 'share/packages/{}'.format(self._pkg.name)

    def get_license(self):
        return self._rls_cfg.license


@contextmanager
def _tarfile_open(name, mode, **kwargs):
    assert mode.startswith('w')
    with tarfile.open(name, mode, **kwargs) as f:
        try:
            yield f
        except:
            os.unlink(name)
            raise


class _FileWithLicense:
    """_FileWithLicense provides a read-only file-like object that automatically includes license text when reading
    from the underlying file object.

    The _FileWithLicense object takes ownership of the underlying file object.
    The original file object should not be used after passing it to the _FileWithLicense object.

    """
    def __init__(self, f, lic, xml_mode):
        XML_PROLOG = b'<?xml version="1.0" encoding="UTF-8" ?>\n'
        self._f = f
        self._read_license = True

        if xml_mode:
            lic = XML_PROLOG + lic
            file_header = f.read(len(XML_PROLOG))
            if file_header != XML_PROLOG:
                raise Exception("XML File: '{}' does not contain expected prolog: {} expected {}".
                                format(f.name, file_header, XML_PROLOG))

        if len(lic) > 0:
            self._read_license = False
            self._license_io = io.BytesIO(lic)

    def read(self, size):
        assert size > 0
        data = b''
        if not self._read_license:
            data = self._license_io.read(size)
            if len(data) < size:
                self._read_license = True
                size -= len(data)

        if self._read_license:
            data += self._f.read(size)

        return data

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()


class _LicenseOpener:
    """The license opener provides a single 'open' method, that can be used instead of the built-in 'open' function.

    This open will return a file-like object that modifies the underlying file to include an appropriate license
    header.

    The 'license' is passed to the object during construction.

    """
    def __init__(self, license):
        self.license = license

    def _get_lic(self, filename):
        lic = ''
        ext = os.path.splitext(filename)[1]
        is_xml = False

        if ext in ['.c', '.h', '.ld', '.s']:
            lic = '/*' + self.license + '*/\n'
        elif ext in ['.py']:
            lic = '"""' + self.license + '"""\n'
        elif ext in ['.prx']:
            lic = '<!--' + self.license + '-->\n'
            is_xml = True
        elif ext in ['.asm']:
            lic = "\n".join(['; ' + line for line in self.license.rsplit("\n")]) + "\n"

        lic = lic.encode('utf8')

        return lic, is_xml

    def open(self, filename, mode):
        assert mode == 'rb'

        f = open(filename, mode)
        lic, is_xml = self._get_lic(filename)
        return _FileWithLicense(f, lic, is_xml)

    def tar_info_filter(self, tarinfo):
        if tarinfo.isreg():
            lic, _ = self._get_lic(tarinfo.name)
            tarinfo.size += len(lic)
        return _tar_info_filter(tarinfo)


def _tar_info_filter(tarinfo):
    tarinfo.uname = '_default_user_'
    tarinfo.gname = '_default_group_'
    tarinfo.mtime = BASE_TIME
    tarinfo.uid = 1000
    tarinfo.gid = 1000
    return tarinfo


def _tar_add_data(tf, arcname, data, ti_filter=None):
    """Directly add data to a tarfile.

    tf is a tarfile.TarFile object.
    arcname is the name the data will have in the archive.
    data is the raw data (which should be of type 'bytes').
    fi_filter filters the created TarInfo object. (In a similar manner to the tarfile.TarFile.add() method.

    """
    ti = tarfile.TarInfo(arcname)
    ti.size = len(data)
    if ti_filter:
        ti = ti_filter(ti)
    tf.addfile(ti, io.BytesIO(data))


def _tar_gz_with_license(output, tree, prefix, license):

    """Create a tar.gz file named `output` from a specified directory tree.

    Any appropriate files have the specified license attached.

    When creating the tar.gz a standard set of meta-data will be used to help ensure things are consistent.

    """
    lo = _LicenseOpener(license)
    try:
        with tarfile.open(output, 'w:gz', format=tarfile.GNU_FORMAT) as tf:
            tarfile.bltn_open = lo.open
            with chdir(tree):
                for f in os.listdir('.'):
                    tf.add(f, arcname='{}/{}'.format(prefix, f), filter=lo.tar_info_filter)
    finally:
        tarfile.bltn_open = open


def _mk_partial(pkg, topdir):
    fn = top_path(topdir, 'release', 'partials', '{}.tar.gz'.format(pkg.get_archive_name()))
    src_prefix = 'share/packages/{}'.format(pkg.get_name())
    _tar_gz_with_license(fn, pkg.get_path(), src_prefix, pkg.get_license())


def build_partials(args):
    build(args)
    os.makedirs(top_path(args.topdir, 'release', 'partials'), exist_ok=True)
    packages = Package.create_from_disk(args.topdir).values()
    for pkg in packages:
        for config in get_release_configs():
            release_package = _ReleasePackage(pkg, config)
            _mk_partial(release_package, args.topdir)


def build_single_release(config, topdir):
    """Build a release archive for a specific release configuration."""
    basename = '{}-{}-{}'.format(config.product_name, config.release_name, config.version)
    logging.info("Building {}".format(basename))
    tarfilename = top_path(topdir, 'release', '{}.tar.gz'.format(basename))
    with _tarfile_open(tarfilename, 'w:gz', format=tarfile.GNU_FORMAT) as tf:
        for pkg in config.packages:
            release_file_path = top_path(topdir, 'release', 'partials', '{}-{}.tar.gz')
            with tarfile.open(release_file_path.format(pkg, config.release_name), 'r:gz') as in_f:
                for m in in_f.getmembers():
                    m_f = in_f.extractfile(m)
                    m.name = basename + '/' + m.name
                    tf.addfile(m, m_f)
        for plat in config.platforms:
            arcname = '{}/{}/bin/prj'.format(basename, plat)
            tf.add('prj_build_{}/prj'.format(plat), arcname=arcname, filter=_tar_info_filter)
        if config.top_level_license is not None:
            _tar_add_data(tf, '{}/LICENSE'.format(basename),
                          config.top_level_license.encode('utf8'),
                          _tar_info_filter)

        for arcname, filename in config.extra_files:
            tf.add(filename, arcname='{}/{}'.format(basename, arcname), filter=_tar_info_filter)

        if 'TEAMCITY_VERSION' in os.environ:
            build_info = os.environ['BUILD_VCS_NUMBER']
        else:
            g = Git()
            build_info = g.branch_hash()
            if not g.working_dir_clean():
                build_info += "-unclean"
        build_info += '\n'
        _tar_add_data(tf, '{}/build_info'.format(basename), build_info.encode('utf8'), _tar_info_filter)


def release_test_one(archive):
    """Test a single archive

    This is primarily a sanity check of the actual release file. Release
    files are only made after other testing has been successfully performed.

    Currently it simply does some sanitfy checking on the tar file to ensure it is consistent.

    In the future additional testing will be performed.

    """
    project_prj_template = """<?xml version="1.0" encoding="UTF-8" ?>
<project>
<search-path>{}</search-path>
</project>
"""

    rel_file = os.path.abspath(archive)
    with tarfile.open(rel_file, 'r:gz') as tf:
        for m in tf.getmembers():
            if m.gid != 1000:
                raise Exception("m.gid != 1000 {} -- {}".format(m.gid, m.name))
            if m.uid != 1000:
                raise Exception("m.uid != 1000 {} -- {}".format(m.uid, m.name))
            if m.mtime != BASE_TIME:
                raise Exception("m.gid != BASE_TIME({}) {} -- {}".format(m.mtime, BASE_TIME, m.name))

    platform = get_host_platform_name()

    with tempdir() as td:
        with chdir(td):
            assert shutil.which('tar')
            subprocess.check_call("tar xf {}".format(rel_file).split())
            release_dir = os.path.splitext(os.path.splitext(os.path.basename(archive))[0])[0]
            if not os.path.isdir(release_dir):
                raise RuntimeError("Release archive does not extract into top directory with the same name as the "
                                   "base name of the archive ({})".format(release_dir))
            with chdir(release_dir):
                cmd = "./{}/bin/prj".format(platform)
                try:
                    subprocess.check_output(cmd, stderr=subprocess.STDOUT, shell=True)
                except subprocess.CalledProcessError as e:
                    if e.returncode != 1:
                        raise e
                pkgs = []
                pkg_root = './share/packages/'
                for root, _, files in os.walk(pkg_root):
                    for f in files:
                        if f.endswith('.prx'):
                            pkg = os.path.join(root, f)[len(pkg_root):-4].replace(os.sep, '.')
                            pkgs.append((pkg, os.path.join(root, f)))
                with open('project.prj', 'w') as f:
                    f.write(project_prj_template.format(pkg_root))
                for pkg, f in pkgs:
                    cmd = "./{}/bin/prj build {}".format(platform, pkg)
                    try:
                        subprocess.check_output(cmd, stderr=subprocess.STDOUT, shell=True)
                    except subprocess.CalledProcessError as e:
                        err_str = None
                        for l in e.output.splitlines():
                            l = l.decode()
                            if l.startswith('ERROR'):
                                err_str = l
                                break
                        if err_str is None:
                            print(e.output)
                            raise e
                        elif 'missing or contains multiple Builder modules' in err_str:
                            pass
                        else:
                            print("Unexpected error:", err_str)
                            raise e


def release_test(args):
    """Implement the test-release command.

    This command is used to perform sanity checks and testing of the full release.

    """
    for rel in glob(top_path(args.topdir, 'release', '*.tar.gz')):
        release_test_one(rel)


def get_release_configs():
    """Return a list of release configs."""
    import release_cfg
    maybe_configs = [getattr(release_cfg, cfg) for cfg in dir(release_cfg)]
    configs = [cfg for cfg in maybe_configs if inspect.isclass(cfg) and issubclass(cfg, Release)]
    enabled_configs = [cfg for cfg in configs if cfg.enabled]
    return enabled_configs


def build_release(args):
    """Implement the build-release command.

    Build release takes the various partial releases, and combines them in to a single tar file.

    Additionally, it takes the binary 'prj' files and adds it to the appropriate place in the release tar file.

    """
    for config in get_release_configs():
        try:
            build_single_release(config, args.topdir)
        except FileNotFoundError as e:
            logging.warning("Unable to build '{}'. File not found: '{}'".format(config, e.filename))