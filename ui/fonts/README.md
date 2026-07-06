# Fonts

Muesli uses the Satoshi typeface by the Indian Type Foundry, distributed via
[Fontshare](https://www.fontshare.com/fonts/satoshi).

Fontshare's free-font EULA does not allow the font files to be redistributed, so
they are not included in this repository or in release downloads. Instead, Muesli
downloads Satoshi from Fontshare's own API the first time it runs (see
`muesli/fonts.py`) and caches it in `%APPDATA%\Muesli\fonts`. If the download is
not possible (offline first run, blocked network), the UI falls back to your
system fonts and everything still works.
