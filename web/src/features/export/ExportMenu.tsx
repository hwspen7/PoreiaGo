import { useEffect, useRef } from 'react';
import { ChevronDown, Download, FileCode2, FileText } from 'lucide-react';
import './exportConversation.css';

type ExportFormat = 'html' | 'pdf';

export function ExportMenu({
  disabled,
  onExport,
}: {
  disabled: boolean;
  onExport: (format: ExportFormat) => void;
}) {
  const menu = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (menu.current && !menu.current.contains(event.target as Node)) menu.current.open = false;
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && menu.current) menu.current.open = false;
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
    };
  }, []);
  useEffect(() => {
    if (disabled && menu.current) menu.current.open = false;
  }, [disabled]);
  const exportSelected = (format: ExportFormat) => {
    if (disabled) return;
    if (menu.current) menu.current.open = false;
    onExport(format);
  };
  return (
    <details ref={menu} className={`topbar-export${disabled ? ' is-disabled' : ''}`}>
      <summary
        aria-label="Export current conversation"
        aria-disabled={disabled}
        onClick={(event) => {
          if (disabled) event.preventDefault();
        }}
      >
        <Download size={15} aria-hidden="true" />
        <span>Export</span>
        <ChevronDown size={13} aria-hidden="true" />
      </summary>
      <div className="topbar-export-menu" role="group" aria-label="Export formats">
        <button type="button" disabled={disabled} onClick={() => exportSelected('html')}>
          <FileCode2 size={17} aria-hidden="true" />
          <span>
            <strong>HTML</strong>
            <small>Download a standalone document</small>
          </span>
        </button>
        <button type="button" disabled={disabled} onClick={() => exportSelected('pdf')}>
          <FileText size={17} aria-hidden="true" />
          <span>
            <strong>PDF</strong>
            <small>Open print dialog to save as PDF</small>
          </span>
        </button>
      </div>
    </details>
  );
}
