import { RefObject, useState } from "react";

/**
 * One explicit copy button with an accessible result announcement. Some Office webviews block the
 * clipboard API: then the text is selected so Ctrl+C works, and the analyst is told so.
 */
export function CopyButton({ text, target, label, done }: { text: string; target: RefObject<HTMLElement | null>; label: string; done: string }) {
  const [copyStatus, setCopyStatus] = useState("");

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopyStatus(done);
    } catch {
      const selection = window.getSelection();
      if (selection && target.current) {
        const range = document.createRange();
        range.selectNodeContents(target.current);
        selection.removeAllRanges();
        selection.addRange(range);
      }
      setCopyStatus("Скопировать автоматически не удалось: текст выделен, нажмите Ctrl+C.");
    }
  }

  return (
    <>
      <button type="button" className="secondary-button" onClick={copy}>{label}</button>
      <span className="copy-status" role="status" aria-live="polite">{copyStatus}</span>
    </>
  );
}
