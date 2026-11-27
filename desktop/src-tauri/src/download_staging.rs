use super::{DownloadDestination, DOWNLOAD_DIRECTORY_CHANGED, DOWNLOAD_WRITE_ERROR};
use rustix::fs::{self, AtFlags, Mode, OFlags, RenameFlags, CWD};
use rustix::io::Errno;
use std::{ffi::OsString, fs::File, io, path::{Path, PathBuf}};

pub(super) struct DownloadStaging {
    // Resolve operations relative to the directory handle so cleanup still works after a move.
    directory: File,
    name: OsString,
}

impl DownloadStaging {
    pub(super) fn create(parent: &Path, destination: &DownloadDestination) -> Result<(File, Self), &'static str> {
        #[cfg(target_os = "linux")]
        let search = OFlags::PATH;
        #[cfg(target_os = "macos")]
        let search = OFlags::from_bits_retain(libc::O_SEARCH as _);
        let directory = File::from(fs::openat(CWD, parent, search | OFlags::DIRECTORY | OFlags::CLOEXEC, Mode::empty())
            .map_err(|_| DOWNLOAD_WRITE_ERROR)?);
        let opened = directory.metadata().map_err(|_| DOWNLOAD_WRITE_ERROR)?;
        if !destination.is_same_directory(&opened) {
            return Err(DOWNLOAD_DIRECTORY_CHANGED);
        }

        let generated = tempfile::Builder::new().disable_cleanup(true).make_in(parent, |path| {
            fs::openat(&directory, path.file_name().unwrap(),
                OFlags::RDWR | OFlags::CREATE | OFlags::EXCL | OFlags::CLOEXEC,
                Mode::RUSR | Mode::WUSR).map(File::from).map_err(io::Error::from)
        }).map_err(|_| DOWNLOAD_WRITE_ERROR)?;
        let (file, path) = generated.into_parts();
        let name = path.file_name().unwrap().to_owned();
        Ok((file, Self { directory, name }))
    }

    pub(super) fn persist(mut self, path: PathBuf) -> io::Result<()> {
        let name = path.file_name().ok_or(io::ErrorKind::InvalidInput)?;
        fs::renameat(&self.directory, &self.name, &self.directory, name)?;
        self.name.clear();
        Ok(())
    }

    pub(super) fn persist_noclobber(mut self, path: PathBuf) -> io::Result<()> {
        let name = path.file_name().ok_or(io::ErrorKind::InvalidInput)?;
        match fs::renameat_with(&self.directory, &self.name, &self.directory, name, RenameFlags::NOREPLACE) {
            Ok(()) => {
                self.name.clear();
                Ok(())
            }
            Err(Errno::NOSYS | Errno::INVAL) => {
                fs::linkat(&self.directory, &self.name, &self.directory, name, AtFlags::empty())?;
                Ok(())
            }
            Err(error) => Err(error.into()),
        }
    }
}

impl Drop for DownloadStaging {
    fn drop(&mut self) {
        if !self.name.is_empty() {
            let _ = fs::unlinkat(&self.directory, &self.name, AtFlags::empty());
        }
    }
}
