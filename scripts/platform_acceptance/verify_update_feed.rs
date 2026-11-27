fn main() {
 let path = std::env::args().nth(1).expect("feed path");
 let release: tauri_plugin_updater::RemoteRelease = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
 assert!(release.download_url("linux-x86_64-deb").unwrap().path().ends_with(".deb"));
 assert!(release.download_url("linux-x86_64-appimage").unwrap().path().ends_with(".AppImage"));
 assert_ne!(release.signature("linux-x86_64-deb").unwrap(), release.signature("linux-x86_64-appimage").unwrap());
 println!("Pinned Tauri updater accepts DEB and AppImage signed target selection");
}
