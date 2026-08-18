import { useRef, useState } from "react";

interface Props {
  onFile: (file: File) => void;
  error: string | null;
}

export default function Dropzone({ onFile, error }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);

  const take = (files: FileList | null) => {
    const file = files?.[0];
    if (file) onFile(file);
  };

  return (
    <div className="dropzone">
      <div
        className={`dropsheet${over ? " over" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          take(e.dataTransfer.files);
        }}
      >
        <div className="dropsheet-inner">
          <div>
            <h1 className="drop-head">Drop an assembly sheet to check it</h1>
            <p className="drop-body">
              The checker reads the balloons off the views and the parts list off the table,
              then compares them item by item. Quantities that disagree, balloons with no
              row, and rows with no balloon are marked on the sheet.
            </p>

            <div className="drop-actions">
              <button type="button" className="btn btn-primary" onClick={() => inputRef.current?.click()}>
                Choose a PDF
              </button>
              <span className="drop-hint">or drag one onto this sheet &middot; single page, vector PDF</span>
              <input
                ref={inputRef}
                type="file"
                accept="application/pdf,.pdf"
                hidden
                onChange={(e) => take(e.target.files)}
              />
            </div>

            {error && (
              <p className="banner error" style={{ marginTop: "1.2rem", border: 0 }}>
                {error}
              </p>
            )}
          </div>

          <dl className="drop-tb">
            <div>
              <dt>Reads</dt>
              <dd>Split balloons</dd>
            </div>
            <div>
              <dt>Reads</dt>
              <dd>ITEM / QTY table</dd>
            </div>
            <div>
              <dt>Checks</dt>
              <dd>Python rules</dd>
            </div>

          </dl>
        </div>
      </div>
    </div>
  );
}
