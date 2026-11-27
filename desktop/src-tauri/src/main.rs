// Prevents an extra console window on Windows in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    #[cfg(target_os = "macos")]
    objc2::rc::autoreleasepool(|_| {
        use objc2::runtime::AnyObject;
        use objc2_foundation::{ns_string, NSDictionary, NSNumber, NSUserDefaults};

        // Kinora restores workspace drafts itself. Skip AppKit's saved window
        // archive before Tauri starts processing the launch Apple event.
        let ignore_state = NSNumber::new_bool(true);
        let defaults = NSDictionary::<_, AnyObject>::from_slices(
            &[ns_string!("ApplePersistenceIgnoreState")],
            &[&ignore_state],
        );
        unsafe { NSUserDefaults::standardUserDefaults().registerDefaults(&defaults) };
    });

    motionfarm_desktop_lib::run()
}
