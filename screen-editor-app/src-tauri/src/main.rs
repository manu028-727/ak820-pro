//! The AK820 Pro's screen layout editor as a desktop app.
//!
//! The editor is the Python one in hostagent/ (stdlib only: it talks to the
//! keyboard through hidraw). This shell starts it on a free localhost port,
//! points its own window at it, and stops it when the window closes. The two
//! .py files travel inside the bundle as resources; python3 comes from the
//! system (the .deb depends on it).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

struct Server(Mutex<Option<Child>>);

fn free_port() -> std::io::Result<u16> {
    Ok(TcpListener::bind("127.0.0.1:0")?.local_addr()?.port())
}

/// Layouts and pictures live where a person can find them.
fn workdir() -> PathBuf {
    let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."));
    let docs = home.join("Documents");
    let base = if docs.is_dir() { docs } else { home };
    base.join("ak820-layouts")
}

fn script(app: &tauri::AppHandle) -> Option<PathBuf> {
    // AK820_LAYOUT_PY overrides, for running against a checkout.
    if let Some(p) = std::env::var_os("AK820_LAYOUT_PY") {
        return Some(PathBuf::from(p));
    }
    let p = app.path().resource_dir().ok()?.join("py").join("ak820layout.py");
    p.is_file().then_some(p)
}

fn python() -> &'static str {
    for py in ["python3", "python"] {
        if Command::new(py).env_remove("PYTHONHOME").env_remove("PYTHONPATH").env_remove("LD_LIBRARY_PATH").arg("--version").stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false) {
            return py;
        }
    }
    "python3"
}

fn start(app: &tauri::AppHandle) -> Result<(Child, u16, PathBuf), String> {
    let script = script(app).ok_or("the editor's Python files are missing from the app")?;
    let dir = workdir();
    std::fs::create_dir_all(&dir).map_err(|e| format!("can't create {}: {e}", dir.display()))?;
    let port = free_port().map_err(|e| format!("no free port: {e}"))?;
    let mut child = Command::new(python())
        .arg(&script)
        .args(["serve", "--no-browser", "--port", &port.to_string(), "--dir"])
        .arg(&dir)
        .stdin(Stdio::null())
        // The AppImage's launcher points these at its own tree, which has
        // no Python standard library: the system python3 must not see them.
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH")
        .env_remove("LD_LIBRARY_PATH")
        .env_remove("LD_PRELOAD")
        .spawn()
        .map_err(|e| format!("can't run python3 ({e}). Install it: sudo apt install python3"))?;
    let t0 = Instant::now();
    while t0.elapsed() < Duration::from_secs(15) {
        if let Ok(Some(st)) = child.try_wait() {
            return Err(format!("the editor stopped at start ({st}). Run it from a terminal to see why: python3 {} serve", script.display()));
        }
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return Ok((child, port, dir));
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    let _ = child.kill();
    Err("the editor did not start within 15 s".into())
}

fn main() {
    tauri::Builder::default()
        .manage(Server(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();
            let win = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("AK820 Screen Editor")
                .inner_size(1280.0, 860.0)
                .min_inner_size(900.0, 600.0)
                .build()?;
            std::thread::spawn(move || match start(&handle) {
                Ok((child, port, dir)) => {
                    *handle.state::<Server>().0.lock().unwrap() = Some(child);
                    let url = format!("http://127.0.0.1:{port}/").parse().unwrap();
                    let _ = win.navigate(url);
                    let _ = win.set_title(&format!("AK820 Screen Editor — {}", dir.display()));
                }
                Err(e) => {
                    let js = format!("document.getElementById('m').textContent={:?}", e);
                    let _ = win.eval(&js);
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the app")
        .run(|app, ev| {
            if let RunEvent::Exit = ev {
                if let Some(mut c) = app.state::<Server>().0.lock().unwrap().take() {
                    let _ = c.kill();
                    let _ = c.wait();
                }
            }
        });
}
