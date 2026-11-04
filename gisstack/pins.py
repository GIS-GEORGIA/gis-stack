"""Pinned SHA-256 checksums of every third-party artefact that install.sh downloads and runs as root (audit S-04).

GeoServer and its extensions publish no checksums, so these were computed by downloading each file once from the
official distribution (SourceForge / build.geoserver.org) on 2026-11-04 and checking that it is a valid zip.  Caddy's
values are the SHA-256 of release tarballs whose SHA-512 matches the checksums published with the release on GitHub.
To bump a version: download the file, verify it against the upstream source, add the new line, remove the old one."""
from __future__ import annotations

CADDY_VERSION = "2.11.8"

PINS = {
    "caddy_2.11.8_linux_amd64.tar.gz": "f032f8805e4df2ce5e1a8a624495c91e9ff763d8ff3ceddadd4784a07c472f40",
    "caddy_2.11.8_linux_arm64.tar.gz": "6781a68e090fecda8c563b5a3b00115e18d7134084099ae804a017622471bed1",
    "geoserver-2.28.6-control-flow-plugin.zip": "fd7b157f394237cb14d0ddfaca0eef9bcce1952880e68542747bf9aacd6b4d01",
    "geoserver-2.28.6-css-plugin.zip": "e9e5931ad155a3f933e2d370854d98f651e9bd58ffcd5a09aac0837a2cc9134b",
    "geoserver-2.28.6-importer-plugin.zip": "79170fdd92ee6771fb6a3a68867ff03888319e917cdafb6a1a3e9b640d23a090",
    "geoserver-2.28.6-inspire-plugin.zip": "486e823f6720674bf8a2ab6b2a8a0ad8678340a6114fd85cdad0f69a1be44b0a",
    "geoserver-2.28.6-mbstyle-plugin.zip": "3c319d53be37d68b6df6bc38fbb79ce5260b2f4d8b7effe2081fdebf134a51f9",
    "geoserver-2.28.6-monitor-plugin.zip": "a08a5d4ec29db7d3a8d23d1df6cff6eb3ad5ac1000d0d48d0d5bae17901311a5",
    "geoserver-2.28.6-ogcapi-features-plugin.zip": "240e4dded84df78192e96fdf3ddfb2e8e34ec546a27888f6b973308e9af67c11",
    "geoserver-2.28.6-vectortiles-plugin.zip": "53ba4ad6a66b316142e781e08003bba3078918ecc292429bf0de74d6751150de",
    "geoserver-2.28.6-war.zip": "a62293f86cfcb648a8850e156fe07cf1294721d2fa1fa44279560c1a285c3450",
    "geoserver-2.28.6-wps-plugin.zip": "b0418dbbc847e8f1b02cbddb1d958dbb29e738ec559c9249839843a2e0ac9124",
    "geoserver-2.28.6-ysld-plugin.zip": "e447cd555bef230833aa1751cde2e814fa15894f6dddc0664dd69c1ded9cfd97",
    "geoserver-3.0.2-control-flow-plugin.zip": "9259c9b46ad602e85bed6ca703b1423acb77bd99ec1a20ac9353a0e1f7bf79b9",
    "geoserver-3.0.2-css-plugin.zip": "fd067a425f883f9aafbeb555c5f0033daa45e55652df7ea53d2700cea68e402d",
    "geoserver-3.0.2-importer-plugin.zip": "b33903ececa5498c8e3552f2ad559492488ae5bca636899f19b2542eaefb07b5",
    "geoserver-3.0.2-inspire-plugin.zip": "79be031c05660777cdf8a4d3bb1f8144ecdb757806c97651813df955972a0d89",
    "geoserver-3.0.2-mbstyle-plugin.zip": "978c018698718d401970583c1522541f2f1548e79af000566ceb5873d5d6d1cf",
    "geoserver-3.0.2-monitor-plugin.zip": "36354d1161880f3ad15652403739e97405da20436377ac42c829d76d87a59e9f",
    "geoserver-3.0.2-ogcapi-features-plugin.zip": "19f67961262bbd7235b201e8ceb72a53254b53df37f69229395cae2a23b8883e",
    "geoserver-3.0.2-vectortiles-plugin.zip": "289bc62c212826650f72873e8c4beac4281b4a241552a3b6dbf533022367745d",
    "geoserver-3.0.2-war.zip": "cfd5713192fad64c0e0e7c89168f29abb798213d5e7229b28274a0c20a736a55",
    "geoserver-3.0.2-wps-plugin.zip": "aaf50ee4943a437dee2620c33263b19e271273d281112dd2494de42b5ffe533e",
    "geoserver-3.0.2-ysld-plugin.zip": "bba58de73e30d48e9cf0d2adc07be2473e94739bb2897e303b783bc21c904764",
}


def pins_file() -> str:
    """The `sha256sum`-style list shipped next to install.sh."""
    return "".join(f"{h}  {n}\n" for n, h in sorted(PINS.items()))
