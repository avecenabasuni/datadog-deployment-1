# Rollback per layer

Rollback dijalankan tim Sucofindo pada window yang disetujui. Tidak ada subcommand
yang menghapus volume/data database. Catat config/image/container ID sebelum dan
sesudah perubahan. DDL MySQL dan installer host tidak transaksional.

## Agent

1. Pastikan container yang dipilih adalah Agent dari change ini, bukan Agent lama.
   Cocokkan name, image, label `id.sucofindo.datadog.managed`, dan inventory.
2. Stop Agent baru jika diperlukan: `docker stop <agent-baru>`. Ini tidak mengubah
   aplikasi/database; telemetry berhenti. Simpan container/config untuk investigasi.
3. Bila tim sebelumnya mengganti Agent secara manual, restore definisi/image/config
   lama lalu start Agent lama setelah Agent baru stop. Jangan menjalankan dua Agent.
4. Untuk disable hanya runtime security/CNM/USM, ubah flag dan review spec baru;
   recreate **Agent saja** oleh tim. Paket menolak perubahan spec existing otomatis.
5. Jika aplikasi masih terinstrumentasi tetapi Agent stop, cek dampak buffering/error
   tracer dan putuskan disable instrumentation bersama rollback aplikasi.

API key baru dapat dicabut sesuai prosedur credential tim setelah tidak digunakan.
Generated MySQL config dan secret local tetap sensitif; kelola retensinya. Jangan
hapus `/var/run/datadog` saat aplikasi/Agent lain masih memakai socket tersebut.

## SSI host dan PHP

1. Untuk menghentikan tracing layanan tertentu, tim memasukkan
   `DD_INSTRUMENT_SERVICE_WITH_APM=false` dan `DD_TRACE_ENABLED=false` pada deployment,
   lalu **recreate** aplikasi untuk menerapkan environment baru.
2. Untuk rollback SSI host secara menyeluruh, ikuti metode uninstall resmi sesuai
   versi installer. Dokumentasi Docker mencantumkan `dd-container-install --uninstall`
   kemudian restart Docker. Pastikan binary tersedia dan review `--help`; layout
   installer modern dapat berada di `/opt/datadog-packages/run/`.
3. Restart Docker memengaruhi container lain: tim menjadwalkan dan menjalankannya,
   bukan script ini. Simpan backup `ssi-before-*` (daemon.json/preload). Jangan copy
   seluruh daemon.json lama membabi buta bila ada perubahan Docker lain sesudah backup.
4. Verifikasi default runtime kembali ke runtime sebelumnya dan config tidak merujuk
   runtime yang sudah dihapus. Tim recreate aplikasi agar injection tidak tertinggal.
5. Pastikan PHP HTTP normal, tidak ada ddtrace ganda, error rate/latency kembali normal.

Jika installer timeout, subprocess downstream bisa masih berjalan. Periksa process,
package state dan Docker runtime sebelum uninstall/rerun. Jangan menjalankan installer
dan uninstall paralel.

## Aplikasi dan startup MySQL

1. Tim mengembalikan env service/version/DBM propagation, socket mounts, serta image
   ke revision deployment sebelumnya. Environment/image/mount baru perlu recreate.
2. Restore file MySQL config sebelum Datadog atau lepas mount tambahan melalui Compose
   tim. Mengganti mount membutuhkan recreate MySQL; jika mount sama, restart cukup
   untuk startup-only variables.
3. **Pertahankan persis volume/data directory database yang sama.** Jangan memakai
   `down -v`, prune volume, remove data directory, atau menginisialisasi DB baru.
4. Verifikasi version/datadir, readiness dan operasi aplikasi setelah restart.
5. Runtime consumers dapat tetap enabled setelah provisioning. DBA mengembalikannya
   ke nilai sebelum change jika diperlukan, bukan mematikan semua consumer tanpa audit.

## SQL DBM yang tetap tertinggal

Stop Agent/restore startup config tidak menghapus account monitoring, grants,
schema `datadog`, procedure `datadog.explain_statement`, procedure runtime consumer,
atau `<schema-aplikasi>.explain_statement`. Mereka tetap ada sampai DBA mengambil
tindakan terpisah. Tidak ada password existing yang diputar script.

DBA membandingkan inventory sebelum/ sesudah dan memastikan tidak ada monitoring
lain yang memakai object tersebut. DBA dapat revoke grant/drop object **yang dibuat
oleh change ini saja** bila tidak diperlukan, setelah review definer/dependency.
Jangan menghapus schema aplikasi atau schema datadog existing secara massal.
Pada provisioning gagal sebagian, baca keadaan account/routines; rerun memverifikasi
object existing dan berhenti jika definisinya berbeda.

## RUM Apache

1. Pilih backup `rum-before-*` dengan container ID yang benar. Jika config test gagal,
   jangan reload sebelum config berhasil diperbaiki. Installer vendor mungkin sudah
   mengubah config; wrapper tidak menjamin transaksi atau rollback otomatis.
2. Tim membandingkan Apache root dengan backup, restore hanya file yang diubah, lalu
   disable/hapus Include/LoadModule atau symlink enable **yang ditambahkan installer**.
   Menyalin backup di atas direktori existing saja tidak menghapus file baru.
3. Pastikan virtual host, TLS, auth dan routing asli tetap ada. Jalankan Apache `-t`;
   jika lulus lakukan graceful reload. Periksa request HTML normal tanpa injection.
4. Jika RUM sudah dipersistenkan, revert image RUM ke revision sebelumnya atau lepaskan
   mount RUM melalui deployment tim, lalu recreate web. Jangan menghapus asset yang
   masih direferensikan container lain.
5. SSI PHP/APM dapat tetap berjalan jika hanya RUM yang dirollback. Tidak perlu
   menghapus PHP tracer manual karena paket ini tidak memasangnya.
6. Restore pengaturan RUM production (tracing URLs/sampling) hanya jika bagian dari
   change ini; simpan config sebelumnya. Application ID development tidak digunakan
   sebagai pengganti.
