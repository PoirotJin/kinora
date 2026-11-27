//! One process per application identity. macOS claims an OS lock before publishing IPC.
use tauri::{plugin::TauriPlugin, AppHandle, Manager, Runtime};

fn activate<R: Runtime>(app: &AppHandle<R>) {
    #[cfg(target_os = "macos")]
    let _ = app.show();
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

#[cfg(not(target_os = "macos"))]
pub fn init<R: Runtime>() -> TauriPlugin<R> {
    tauri_plugin_single_instance::init(|app, _, _| activate(app))
}

#[cfg(target_os = "macos")]
pub fn init<R: Runtime>() -> TauriPlugin<R> {
    macos::init()
}

#[cfg(target_os = "macos")]
mod macos {
    use super::*;
    use sha2::{Digest, Sha256};
    use std::{
        fs::{self, DirBuilder, File, OpenOptions},
        io::{self, Read, Write},
        os::{fd::AsRawFd, unix::{fs::{DirBuilderExt, MetadataExt, OpenOptionsExt}, net::{UnixListener, UnixStream}}},
        path::{Path, PathBuf},
        sync::{atomic::{AtomicBool, Ordering}, Arc},
        time::{Duration, Instant},
    };
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    const MESSAGE: &[u8; 9] = b"activate\n";
    const ACK: &[u8; 3] = b"ok\n";

    struct Primary {
        socket: PathBuf,
        _lock: File,
        listener_task: tauri::async_runtime::JoinHandle<()>,
    }

    impl tauri::Resource for Primary {}

    impl Drop for Primary {
        fn drop(&mut self) {
            self.listener_task.abort();
            // Keep the lock held until the socket has been removed. Never unlink the lock file.
            let _ = fs::remove_file(&self.socket);
        }
    }

    fn private_directory() -> io::Result<PathBuf> {
        let uid = unsafe { libc::geteuid() };
        // Short enough for macOS Unix socket paths, even for long application identifiers.
        let directory = PathBuf::from(format!("/tmp/kinora-single-instance-{uid}"));
        match DirBuilder::new().mode(0o700).create(&directory) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error),
        }
        let metadata = fs::symlink_metadata(&directory)?;
        if !metadata.is_dir() || metadata.uid() != uid || metadata.mode() & 0o777 != 0o700 {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "Unsafe single-instance directory"));
        }
        Ok(directory)
    }

    fn lock_file(path: &Path) -> io::Result<File> {
        let file = OpenOptions::new().read(true).write(true).create(true)
            .mode(0o600).custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC).open(path)?;
        let metadata = file.metadata()?;
        if !metadata.is_file() || metadata.nlink() != 1 || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.mode() & 0o777 != 0o600 {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "Unsafe single-instance lock"));
        }
        Ok(file)
    }

    fn try_lock(lock: &File) -> io::Result<bool> {
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
            return Ok(true);
        }
        let error = io::Error::last_os_error();
        match error.kind() {
            io::ErrorKind::WouldBlock => Ok(false),
            _ => Err(error),
        }
    }

    fn notify_or_take_lock(socket: PathBuf, lock: File) -> io::Result<Option<File>> {
        // UnixStream::connect can itself block on a full backlog. Bound the entire operation.
        let (sender, receiver) = std::sync::mpsc::sync_channel(1);
        std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(5);
            loop {
                match try_lock(&lock) {
                    Ok(true) => {
                        let _ = sender.send(Ok(Some(lock)));
                        return;
                    }
                    Ok(false) => {}
                    Err(error) => {
                        let _ = sender.send(Err(error));
                        return;
                    }
                }
                let result = (|| {
                    let mut stream = UnixStream::connect(&socket)?;
                    stream.set_read_timeout(Some(Duration::from_millis(300)))?;
                    stream.set_write_timeout(Some(Duration::from_millis(300)))?;
                    stream.write_all(MESSAGE)?;
                    let mut ack = [0; 3];
                    stream.read_exact(&mut ack)?;
                    if &ack != ACK { return Err(io::Error::other("Invalid single-instance response")); }
                    Ok(())
                })();
                if result.is_ok() || Instant::now() >= deadline {
                    let _ = sender.send(result.map(|_| None));
                    return;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
        });
        receiver.recv_timeout(Duration::from_secs(6))
            .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "Existing kinora instance did not respond"))?
    }

    pub fn init<R: Runtime>() -> TauriPlugin<R> {
        let pending = Arc::new(AtomicBool::new(false));
        let ready_pending = pending.clone();
        tauri::plugin::Builder::new("single-instance")
            .setup(move |app, _| {
                let directory = private_directory()?;
                let digest = format!("{:x}", Sha256::digest(app.config().identifier.as_bytes()));
                let key = &digest[..32];
                let lock = lock_file(&directory.join(format!("{key}.lock")))?;
                let socket = directory.join(format!("{key}.sock"));
                let lock = if try_lock(&lock)? {
                    lock
                } else {
                    // During restart the existing process can exit before acknowledging.
                    match notify_or_take_lock(socket.clone(), lock) {
                        Ok(Some(lock)) => lock,
                        Ok(None) => std::process::exit(0),
                        Err(error) => {
                            // This launch never became the owner. Keep the original instance
                            // intact and avoid turning a notification timeout into a panic.
                            eprintln!("Cannot activate the existing Kinora instance: {error}");
                            std::process::exit(1);
                        }
                    }
                };
                match fs::remove_file(&socket) {
                    Ok(()) => {}
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error.into()),
                }
                // Binding happens while holding the lock, before setup returns or a window exists.
                let listener = UnixListener::bind(&socket)?;
                listener.set_nonblocking(true)?;
                let resources_app = app.clone();
                let app = app.clone();
                let pending = pending.clone();
                let listener_task = tauri::async_runtime::spawn(async move {
                    let listener = match tokio::net::UnixListener::from_std(listener) {
                        Ok(listener) => listener,
                        Err(error) => { eprintln!("Single-instance listener failed: {error}"); app.exit(1); return; }
                    };
                    let slots = Arc::new(tokio::sync::Semaphore::new(16));
                    loop {
                        let (mut stream, _) = match listener.accept().await {
                            Ok(connection) => connection,
                            Err(error) => { eprintln!("Single-instance accept failed: {error}"); app.exit(1); return; }
                        };
                        let Ok(permit) = slots.clone().try_acquire_owned() else { continue };
                        let app = app.clone();
                        let pending = pending.clone();
                        tauri::async_runtime::spawn(async move {
                            let _permit = permit;
                            let _ = tokio::time::timeout(Duration::from_millis(500), async {
                                let mut message = [0; 9];
                                stream.read_exact(&mut message).await?;
                                if &message != MESSAGE { return Err(io::Error::other("Invalid activation request")); }
                                pending.store(true, Ordering::SeqCst);
                                let target = app.clone();
                                app.run_on_main_thread(move || activate(&target))
                                    .map_err(io::Error::other)?;
                                stream.write_all(ACK).await
                            }).await;
                        });
                    }
                });
                // Tauri drains resources before restart, while managed state can outlive it.
                resources_app.resources_table().add(Primary { socket, _lock: lock, listener_task });
                Ok(())
            })
            .on_event(move |app, event| {
                if matches!(event, tauri::RunEvent::Ready) && ready_pending.swap(false, Ordering::SeqCst) {
                    activate(app);
                }
            })
            .build()
    }

    #[cfg(test)]
    #[test]
    fn launch_takes_over_after_unacknowledged_activation() {
        let directory = tempfile::tempdir_in("/tmp").unwrap();
        let path = directory.path().join("instance.lock");
        let socket = directory.path().join("instance.sock");
        let old_lock = lock_file(&path).unwrap();
        assert!(try_lock(&old_lock).unwrap());
        let listener = UnixListener::bind(&socket).unwrap();
        let new_lock = lock_file(&path).unwrap();
        let notify_socket = socket.clone();
        let replacement = std::thread::spawn(move || notify_or_take_lock(notify_socket, new_lock));

        listener.set_nonblocking(true).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        let mut connection = loop {
            match listener.accept() {
                Ok((stream, _)) => break stream,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock && Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(10));
                }
                Err(error) => panic!("replacement did not contact primary: {error}"),
            }
        };
        connection.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let mut activation = [0; MESSAGE.len()];
        connection.read_exact(&mut activation).unwrap();
        assert_eq!(&activation, MESSAGE);
        fs::remove_file(socket).unwrap();
        drop(listener);
        drop(old_lock);
        drop(connection);

        let replacement_lock = replacement.join().unwrap().unwrap().expect("replacement should own the lock");
        assert!(!try_lock(&lock_file(&path).unwrap()).unwrap());
        drop(replacement_lock);
        assert!(try_lock(&lock_file(&path).unwrap()).unwrap());
    }
}
