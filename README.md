# Scorex Backup — PostgreSQL → SharePoint (2 giờ/lần)

## Thành phần

| File | Chức năng |
|---|---|
| `scripts/backup_postgres.sh` | `pg_dump` + gzip database, lưu vào `dumps/`, dọn file cục bộ cũ hơn 24h |
| `scripts/upload_sharepoint.py` | Lấy token Azure AD (client credentials), upload file lên SharePoint qua Microsoft Graph (tự chuyển sang upload session nếu file > 4MB) |
| `scripts/run_backup.sh` | Gọi lần lượt 2 script trên, ghi log |
| `.env.example` | Cấu hình DB + Azure AD + đường dẫn SharePoint (đã điền sẵn theo thông tin bạn cung cấp, nhắm tới `HDBANK/PRD`) |
| `systemd/auto-backup.service` | Unit chạy `run_backup.sh` với user `backup_user` |
| `systemd/auto-backup.timer` | Kích hoạt service mỗi 2 giờ (`OnCalendar=*-*-* 00/2:00:00`) |
| `install.sh` | Cài đặt toàn bộ: tạo user, thư mục, **file log với quyền đúng trước khi chạy lần đầu**, copy file, bật timer |

## Cài đặt trên VPS (local_auto)

```bash
# copy thư mục này lên VPS, ví dụ qua scp
mkdir backup_db

cd /backup_db
chmod +x install.sh scripts/*.sh
sudo ./install.sh
```

Script `install.sh` sẽ:
1. Tạo user hệ thống `backup_user` (không có quyền login shell)
2. Tạo `/opt/auto-backup/{scripts,dumps}`
3. **Tạo `/var/log/auto-backup.log` và set quyền cho `backup_user` trước khi service chạy lần đầu** — đây là bước quan trọng để tránh lỗi `PermissionError` đã gặp trước đó
4. Copy `.env.example` → `/opt/auto-backup/.env` (nếu chưa có `.env` sẵn)
5. Cài `requests` cho Python
6. Cài và bật `auto-backup.timer`

## Kiểm tra sau khi cài

```bash
systemctl status auto-backup.timer
systemctl list-timers auto-backup.timer      # xem lần chạy kế tiếp
sudo -u backup_user /opt/auto-backup/scripts/run_backup.sh   # chạy thử ngay
tail -f /var/log/auto-backup.log
```

## Đổi sang môi trường DEV

Nếu cần một job backup riêng cho DEV, tạo thêm 1 bộ `.env` với:
```
SHAREPOINT_FOLDER=<Folder_backup_sharepoint)
POSTGRES_DATABASE=<tên db>
```
và một cặp service/timer riêng (đổi tên, đổi `ExecStart` trỏ tới thư mục cấu hình DEV).

## Lưu ý bảo mật

Các thông tin nhạy cảm (mật khẩu DB, Client Secret Azure AD, mật khẩu SSH/SharePoint) đã được dán trực tiếp vào cấu hình `.env` để tiện triển khai. Vì các giá trị này đã xuất hiện trong hội thoại, bạn nên cân nhắc xoay vòng (rotate) Client Secret và các mật khẩu liên quan sau khi triển khai xong, và giữ file `.env` ở quyền `600` (script đã tự set).
