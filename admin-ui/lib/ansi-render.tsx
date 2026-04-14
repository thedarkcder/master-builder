import { Fragment, type ReactNode } from "react";

const ANSI_SGR_PATTERN = /\x1b\[([0-9;]*)m/g;
const URL_PATTERN = /(https?:\/\/[^\s]+)/g;

type TextStyle = {
  className?: string;
};

function styleForSgrCode(code: string): TextStyle {
  if (code === "90") {
    return { className: "text-muted-foreground" };
  }
  if (code === "94") {
    return { className: "text-sky-700 dark:text-sky-300" };
  }
  return {};
}

function renderTextSegment(text: string, keyPrefix: string, className?: string): ReactNode[] {
  return text.split(URL_PATTERN).map((segment, index) => {
    if (!segment) {
      return null;
    }
    if (segment.startsWith("http://") || segment.startsWith("https://")) {
      return (
        <a
          key={`${keyPrefix}-url-${index}`}
          href={segment}
          target="_blank"
          rel="noreferrer"
          className={className ? `${className} underline underline-offset-2` : "underline underline-offset-2"}
        >
          {segment}
        </a>
      );
    }
    if (className) {
      return (
        <span key={`${keyPrefix}-text-${index}`} className={className}>
          {segment}
        </span>
      );
    }
    return <Fragment key={`${keyPrefix}-text-${index}`}>{segment}</Fragment>;
  });
}

export function renderAnsiText(text: string): ReactNode {
  const output: ReactNode[] = [];
  let cursor = 0;
  let activeStyle: TextStyle = {};
  let matchIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = ANSI_SGR_PATTERN.exec(text)) !== null) {
    const [fullMatch, rawCodes] = match;
    const chunk = text.slice(cursor, match.index);
    if (chunk) {
      output.push(...renderTextSegment(chunk, `ansi-${matchIndex}`, activeStyle.className));
    }
    const codes = (rawCodes || "0")
      .split(";")
      .map((part) => part.trim())
      .filter(Boolean);
    if (codes.length === 0 || codes.includes("0")) {
      activeStyle = {};
    } else {
      for (const code of codes) {
        const nextStyle = styleForSgrCode(code);
        if (nextStyle.className) {
          activeStyle = nextStyle;
        }
      }
    }
    cursor = match.index + fullMatch.length;
    matchIndex += 1;
  }

  const tail = text.slice(cursor);
  if (tail) {
    output.push(...renderTextSegment(tail, `ansi-tail-${matchIndex}`, activeStyle.className));
  }

  return output;
}
