/**
 * Human text for backend reason codes (UI-UX §38, §43). Codes come from
 * backend/app/data/ingestion (error `details.reason`, `DatasetVersion.failure.code`) and the
 * `review_flags` written by ingestion / profiling / understanding (`CODE` or `CODE:column`).
 * Unknown codes fall back to the backend message (errors) or the raw code (flags).
 */
const REJECTION_TEXT: Record<string, string> = {
  MACRO_ENABLED:
    "This file type can't be processed safely: macro-enabled workbooks aren't supported. Save it as a plain .xlsx or .csv and upload again.",
  ZIP_BOMB:
    "This workbook expands to far more data than its file size suggests, so it can't be processed safely. Re-save it from your spreadsheet application and try again.",
  OVERSIZED_FILE:
    'The file is larger than the upload limit. Split it or remove unused columns and try again.',
  UNSUPPORTED_FORMAT: "This file format isn't supported. Upload a .csv or .xlsx file.",
  CORRUPT_ARCHIVE:
    "The workbook appears to be damaged and couldn't be opened. Re-save it and try again.",
  UNSAFE_ARCHIVE_PATH:
    "The workbook contains an unsafe internal file path, so it can't be processed safely.",
  EMPTY_FILE: 'The file has no rows to import.',
  NO_DATA_ROWS: 'The file has a header row but no data rows.',
  MALFORMED_CSV:
    "The file couldn't be read as CSV (for example, an unclosed quote). Check the file and try again.",
  TOO_MANY_BAD_ROWS:
    "Too many rows couldn't be read, so the import was stopped. Check the column count and quoting.",
  TOO_MANY_ROWS: 'The file has more rows than this instance accepts.',
  TYPE_CONVERSION_FAILED: "Some values couldn't be converted to a consistent column type.",
  SHEET_NOT_FOUND: "The selected sheet wasn't found in the workbook.",
  CANCELLED: 'Processing was cancelled.',
};

/** Plain-language reason for a rejected upload or failed processing step. */
export function rejectionText(reason: string | null | undefined, fallback: string): string {
  return (reason && REJECTION_TEXT[reason]) || fallback;
}

type FlagText = (column: string) => string;

const FLAG_TEXT: Record<string, FlagText> = {
  DELIMITER_NOT_COMMA: () =>
    'The file uses a separator other than a comma; it was detected automatically. Check that the columns split correctly.',
  NO_HEADER_ROW: () => 'No header row was detected, so column names were generated.',
  MULTIPLE_SHEETS: () => 'The workbook has several sheets; only one sheet was imported.',
  FORMULA_CELLS_PRESENT: () =>
    'The workbook contains formulas. Their stored values were imported; formulas were not run.',
  FORMULA_INJECTION_RISK: (c) =>
    `Some values in "${c}" start with a character spreadsheet programs treat as a formula (=, +, -, @). They are stored as plain text and never run.`,
  MISSING_VALUE_TOKENS: () =>
    'Placeholder values such as "N/A", "null" or "-" were treated as missing.',
  DATE_FORMAT_DMY: (c) => `Dates in "${c}" were read as day/month/year. Confirm this is right.`,
  DATE_FORMAT_MDY: (c) => `Dates in "${c}" were read as month/day/year. Confirm this is right.`,
  MIXED_DATE_FORMATS: (c) => `"${c}" mixes date formats, so some dates may be misread.`,
  EMPTY_COLUMN_NAME: (c) => `A column had no name; it was named "${c}".`,
  DUPLICATE_COLUMN_NAMES: (c) =>
    `Several columns were named "${c}"; they were renamed to be unique.`,
  ENCODING_NON_UTF8: () =>
    "The file isn't UTF-8; its text encoding was detected automatically. Check accented or special characters.",
  EXTENSION_MISMATCH: () =>
    "The file's content doesn't match its extension; it was read according to its content.",
  MALFORMED_ROW_FIELD_COUNT: () =>
    'Some rows had the wrong number of fields and were set aside. See the rejected rows below.',
  DUPLICATE_ROWS: () => 'The data contains duplicate rows.',
  ALL_NULL_COLUMN: (c) => `"${c}" is empty in every row.`,
  NO_PRIMARY_KEY: () => 'No column uniquely identifies each row.',
  TYPE_UNCERTAIN: (c) => `The type of "${c}" is uncertain.`,
  CURRENCY_UNKNOWN: (c) => `"${c}" looks like money, but its currency is unknown.`,
  MAPPING_AMBIGUOUS: (c) => `The business meaning of "${c}" is ambiguous.`,
  TIME_GRAIN_UNKNOWN: (c) => `The time granularity of "${c}" couldn't be determined.`,
};

export interface FlagExplanation {
  code: string;
  column: string | null;
  text: string;
}

/** Splits `CODE:column` and explains it; unknown codes are shown as-is, never hidden. */
export function explainFlag(flag: string): FlagExplanation {
  const separator = flag.indexOf(':');
  const code = separator === -1 ? flag : flag.slice(0, separator);
  const column = separator === -1 ? null : flag.slice(separator + 1);
  const explain = FLAG_TEXT[code];
  return {
    code,
    column,
    text: explain ? explain(column ?? '') : `Needs review: ${flag}`,
  };
}
