# AK820 Screen Editor (desktop app)

The screen layout editor (`hostagent/ak820layout_editor.py`) in its own window.
A small Tauri shell starts the Python editor on a free localhost port, shows it,
and stops it when the window closes. Layouts and pictures are kept in
`~/Documents/ak820-layouts`.

Linux only for now (the upload goes through hidraw). Needs `python3` on the
system; nothing else (the editor is stdlib only, Pillow is used if present).

## Install

    sudo apt install ./"AK820 Screen Editor_0.1.0_amd64.deb"

or run the AppImage directly (`chmod +x` it first).

## Build

    sudo apt install libwebkit2gtk-4.1-dev build-essential libxdo-dev libssl-dev librsvg2-dev
    npm i -g @tauri-apps/cli@2      # or: cargo install tauri-cli --version '^2'
    cd screen-editor-app/src-tauri && tauri build

The bundles land in `src-tauri/target/release/bundle/{deb,appimage}/`. The two
`.py` files and `assets-src/current/flash_assets.bin` are copied in as
resources, so rebuild after changing them. `AK820_LAYOUT_PY=/path/to/ak820layout.py`
runs the app against a checkout instead.
