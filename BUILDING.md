# Building the .exe / .dmg

**PyInstaller cannot cross-compile.** A Windows `.exe` must be built on Windows;
a macOS `.dmg` must be built on macOS. There is no way around this.

## Option A — GitHub Actions (recommended, no second machine needed)

Push this repo to GitHub. The workflow in `.github/workflows/build.yml` builds
both on native runners.

    git tag v1.0.0 && git push --tags

Or run it manually from the Actions tab. Download `TradingBot-windows.zip` and
`TradingBot-macos.dmg` from the run's artifacts. Free for public repos.

## Option B — build locally

    pip install -r requirements.txt pyinstaller
    pyinstaller tradingbot.spec --clean --noconfirm

Windows → `dist/TradingBot/TradingBot.exe`
macOS   → `dist/TradingBot.app`, then:

    hdiutil create -volname TradingBot -srcfolder dist/TradingBot.app \
      -ov -format UDZO TradingBot.dmg

---

## Things that will bite you

### Antivirus false positives (Windows)
A PyInstaller binary that makes network calls and handles crypto keys trips
heuristic detection constantly. Expect SmartScreen warnings and possible
quarantine. Mitigations, in order of effectiveness:

- **Code-sign it.** An OV certificate is ~$200–400/yr; EV certs get SmartScreen
  reputation immediately. This is the only real fix.
- `upx=False` is already set in the spec — UPX packing is a major AV trigger.
- Submit false positives to vendors.
- Ship `--onedir` (already configured), not `--onefile`. One-file builds unpack
  to temp at runtime, which looks exactly like malware behaviour.

### Gatekeeper (macOS)
An unsigned `.app` will refuse to open — users get "damaged and can't be
opened." Options:

- Right-click → Open, once, to bypass. Fine for personal use.
- Proper fix: Apple Developer account ($99/yr), then sign and notarize:

      codesign --deep --force --options runtime \
        --sign "Developer ID Application: YOUR NAME (TEAMID)" dist/TradingBot.app
      xcrun notarytool submit TradingBot.dmg --apple-id ... --wait
      xcrun stapler staple TradingBot.dmg

- For Apple Silicon + Intel, set `target_arch="universal2"` in the spec and
  build with a universal2 Python.

### Size
~130MB unpacked. `solders` alone is 35MB of compiled Rust. Normal for this
dependency set.

---

## Do not ship your keys

The launcher holds credentials **in memory for the session only** and writes
nothing to disk. Keep it that way.

A packaged binary is a single file that gets copied, synced to cloud storage,
and forwarded. Anything baked into it travels with it. If you distribute this
build to anyone, distribute it empty — and understand that whoever runs it can
read any key you typed into their copy.

For unattended operation, set credentials as OS environment variables outside
the app, or use a hardware signer. Not a config file next to the executable.
