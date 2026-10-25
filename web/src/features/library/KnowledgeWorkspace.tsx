import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import {
  BookOpen,
  CheckCircle2,
  FileText,
  Info,
  Plus,
  Search,
  Trash2,
  UploadCloud,
  X,
} from 'lucide-react';
import { apiJSON } from '../../lib/api';
import './knowledgeWorkspace.css';

type DocumentItem = { id: number; title: string; size: number };
type SearchHit = { title: string; document_id: number; text: string; score?: number };
type SearchResponse = { hits: SearchHit[]; mode: string; limitations?: string[] };
const MAX_CHARS = 150000;

export function KnowledgeWorkspace({ userId }: { userId: string }) {
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [searchResult, setSearchResult] = useState<SearchResponse | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const refresh = useCallback(async () => {
    const data = await apiJSON<{ documents: DocumentItem[] }>(
      `/api/knowledge?user_id=${encodeURIComponent(userId)}`,
    );
    setDocuments(data.documents);
  }, [userId]);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    apiJSON<{ documents: DocumentItem[] }>(`/api/knowledge?user_id=${encodeURIComponent(userId)}`)
      .then((r) => {
        if (!cancelled) {
          setDocuments(r.documents);
          setError('');
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load your library');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);
  async function uploadFile(file: File | undefined) {
    if (!file) return;
    setError('');
    setNotice('');
    if (!/\.(txt|md|markdown)$/i.test(file.name)) {
      setError('Please choose a .txt or .md file');
      return;
    }
    if (file.size > MAX_CHARS * 4) {
      setError('This file is too large for the knowledge library');
      return;
    }
    try {
      const text = await file.text();
      if (!text.trim() || text.length > MAX_CHARS) {
        setError('Document must contain 1 to 150,000 characters');
        return;
      }
      setTitle(file.name.replace(/\.(txt|md|markdown)$/i, '').slice(0, 200));
      setContent(text);
      setEditorOpen(true);
    } catch {
      setError('Could not read this file');
    }
    if (fileInput.current) fileInput.current.value = '';
  }
  async function saveDocument() {
    const normalizedTitle = title.trim();
    const normalizedContent = content.trim();
    if (!normalizedTitle || !normalizedContent) {
      setError('Add a title and some text before saving');
      return;
    }
    if (normalizedTitle.length > 200 || normalizedContent.length > MAX_CHARS) {
      setError('Title or content exceeds the supported length');
      return;
    }
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await apiJSON('/api/knowledge/documents', {
        method: 'POST',
        body: JSON.stringify({
          user_id: userId,
          title: normalizedTitle,
          content: normalizedContent,
        }),
      });
      await refresh();
      setTitle('');
      setContent('');
      setEditorOpen(false);
      setNotice('Document added to your knowledge library');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save this document');
    } finally {
      setBusy(false);
    }
  }
  async function deleteDocument(document: DocumentItem) {
    if (!window.confirm(`Remove "${document.title}" from your knowledge library?`)) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await apiJSON(
        `/api/knowledge/documents/${document.id}?user_id=${encodeURIComponent(userId)}`,
        { method: 'DELETE' },
      );
      await refresh();
      setSearchResult(null);
      setSearched(false);
      setNotice('Document removed');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not remove this document');
    } finally {
      setBusy(false);
    }
  }
  async function runSearch(e: FormEvent) {
    e.preventDefault();
    if (!query.trim()) return;
    setSearching(true);
    setError('');
    setNotice('');
    try {
      const result = await apiJSON<SearchResponse>(
        `/api/knowledge/search?user_id=${encodeURIComponent(userId)}&k=5&query=${encodeURIComponent(query.trim())}`,
      );
      setSearchResult(result);
      setSearched(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Search failed');
    } finally {
      setSearching(false);
    }
  }
  return (
    <div className="dashboard knowledge-workspace">
      <header className="page-heading knowledge-heading">
        <div className="eyebrow">YOUR REFERENCE LIBRARY</div>
        <h1>Travel Library</h1>
        <p>
          Add travel notes and reference material that PoreiaGo can consult when planning your
          journeys
        </p>
      </header>
      <div className="knowledge-intro">
        <span className="knowledge-intro-icon">
          <BookOpen size={21} />
        </span>
        <div>
          <strong>Keep useful travel information in one place</strong>
          <p>
            Save your research, destination guides, and practical notes so relevant details are
            easier to find and reuse
          </p>
        </div>
      </div>
      <div className="knowledge-grid">
        <section className="knowledge-panel knowledge-library">
          <div className="knowledge-panel-title">
            <div>
              <FileText size={18} />
              <h2>Your documents</h2>
            </div>
            <span>{documents.length}</span>
          </div>
          <div className="knowledge-list" aria-live="polite">
            {loading ? (
              <div className="knowledge-empty">Loading your library...</div>
            ) : documents.length ? (
              documents.map((d) => (
                <div className="knowledge-document" key={d.id}>
                  <span className="knowledge-file-icon">
                    <FileText size={18} />
                  </span>
                  <div>
                    <strong>{d.title}</strong>
                    <small>{d.size.toLocaleString('en-US')} characters</small>
                  </div>
                  <button
                    type="button"
                    disabled={busy}
                    className="knowledge-delete"
                    onClick={() => deleteDocument(d)}
                    aria-label={`Delete ${d.title}`}
                    title="Remove document"
                  >
                    <Trash2 size={16} />
                  </button>
                </div>
              ))
            ) : (
              <div className="knowledge-empty">
                <FileText size={29} />
                <strong>No documents yet</strong>
                <p>Add a guide or travel note to start your library</p>
              </div>
            )}
          </div>
          <button
            type="button"
            className="knowledge-primary"
            onClick={() => setEditorOpen((o) => !o)}
          >
            <Plus size={16} />
            {editorOpen ? 'Close editor' : 'Add document'}
          </button>
          <input
            ref={fileInput}
            type="file"
            accept=".txt,.md,.markdown,text/plain,text/markdown"
            style={{ display: 'none' }}
            onChange={(e) => {
              void uploadFile(e.target.files?.[0]);
            }}
          />
          <button
            type="button"
            className="knowledge-secondary"
            onClick={() => fileInput.current?.click()}
          >
            <UploadCloud size={16} /> Import TXT or Markdown
          </button>
        </section>
        <section className="knowledge-panel knowledge-search-panel">
          <div className="knowledge-panel-title">
            <div>
              <Search size={18} />
              <h2>Search your knowledge</h2>
            </div>
          </div>
          <p className="knowledge-panel-description">
            Find relevant passages from your saved documents, including offline text search
          </p>
          <form className="knowledge-search" onSubmit={runSearch}>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search for a place, rule, or travel detail"
              aria-label="Search your documents"
            />
            <button disabled={searching || !query.trim()} type="submit">
              {searching ? 'Searching...' : 'Search'}
            </button>
          </form>
          <div className="knowledge-hits" aria-live="polite">
            {searched && !searchResult?.hits.length ? (
              <div className="knowledge-empty compact">No matching passages found</div>
            ) : (
              (searchResult?.hits ?? []).map((hit, i) => (
                <div className="knowledge-hit" key={`${hit.document_id}-${i}`}>
                  <strong>{hit.title}</strong>
                  <p>{hit.text.length > 420 ? hit.text.slice(0, 420) + '…' : hit.text}</p>
                </div>
              ))
            )}
          </div>
          {searched && searchResult?.limitations?.length ? (
            <div className="knowledge-search-note">
              <Info size={14} />
              {searchResult.limitations.join(' ')}
            </div>
          ) : null}
        </section>
      </div>
      {editorOpen && (
        <section className="knowledge-panel knowledge-editor">
          <div className="knowledge-panel-title">
            <div>
              <Plus size={18} />
              <h2>Add a reference</h2>
            </div>
            <button
              type="button"
              className="knowledge-delete"
              onClick={() => setEditorOpen(false)}
              aria-label="Close editor"
            >
              <X size={17} />
            </button>
          </div>
          <p className="knowledge-panel-description">
            Paste travel research, hotel notes, or reliable information you want to keep for later
          </p>
          <label htmlFor="knowledge-title">Document title</label>
          <input
            id="knowledge-title"
            maxLength={200}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="e.g. Kyoto transport notes"
          />
          <label htmlFor="knowledge-content">Reference text</label>
          <textarea
            id="knowledge-content"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            maxLength={MAX_CHARS}
            rows={8}
            placeholder="Paste your notes or source text here..."
          />
          <div className="knowledge-editor-actions">
            <small>
              {content.length.toLocaleString('en-US')} / {MAX_CHARS.toLocaleString('en-US')}{' '}
              characters
            </small>
            <button
              type="button"
              className="knowledge-primary"
              onClick={saveDocument}
              disabled={busy || !title.trim() || !content.trim()}
            >
              {busy ? 'Saving...' : 'Save document'}
            </button>
          </div>
        </section>
      )}
      {error && (
        <div className="knowledge-message error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="knowledge-message success" role="status">
          <CheckCircle2 size={15} />
          {notice}
        </div>
      )}
      <div className="knowledge-privacy">
        <Info size={16} />
        <span>
          Documents are stored for the current user; source quality and availability still need
          independent verification
        </span>
      </div>
    </div>
  );
}
