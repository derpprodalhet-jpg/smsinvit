import { useRef, useState } from 'react';
import { useDialogFocus } from '../lib/dialog-focus';

const QR_SRC = '/support-qr.png';
const GROUP_HANDLE = '@LINGGCARDSUPPORT';
const GROUP_URL = 'https://t.me/lingcardsupport';

/**
 * 右下角常驻「交流群」入口：点开是 Telegram 群二维码，方便用户扫码入群。
 * 图片放在 apps/web/public 下，构建后由 SPA 同源提供（/support-qr.png）。
 */
export function SupportGroupEntry() {
  const [open, setOpen] = useState(false);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  useDialogFocus(open, dialogRef, () => setOpen(false));

  return (
    <>
      <button
        type="button"
        className="support-entry"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
      >
        交流群
      </button>
      {open && (
        <div className="support-backdrop" onClick={() => setOpen(false)}>
          <div
            ref={dialogRef}
            className="support-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="加入交流群"
            onClick={(event) => event.stopPropagation()}
          >
            <h2 className="support-dialog-title">加入交流群</h2>
            <p className="support-dialog-hint">用手机扫码，或在 Telegram 搜索群名。</p>
            <img className="support-qr" src={QR_SRC} alt={`Telegram 群二维码 ${GROUP_HANDLE}`} />
            <p className="support-handle">
              <a href={GROUP_URL} target="_blank" rel="noreferrer noopener">
                {GROUP_HANDLE}
              </a>
            </p>
            <div className="support-actions">
              <a className="support-primary" href={GROUP_URL} target="_blank" rel="noreferrer noopener">
                在 Telegram 打开
              </a>
              <button type="button" className="support-secondary" onClick={() => setOpen(false)}>
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
