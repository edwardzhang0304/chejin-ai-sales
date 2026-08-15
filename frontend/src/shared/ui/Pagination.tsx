type PaginationItem = number | "...";

type Props = {
  as?: "div" | "footer";
  ariaLabel: string;
  className?: string;
  currentPage: number;
  disabled?: boolean;
  ellipsis?: "..." | "…";
  pageSize: number;
  pageSizeAriaLabel: string;
  pageSizeOptions: readonly number[];
  spacedPageSizeLabel?: boolean;
  splitTotal?: boolean;
  total: number;
  totalPages: number;
  totalUnit: string;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
};

export function getPaginationItems(currentPage: number, totalPages: number): PaginationItem[] {
  if (totalPages <= 5) {
    return Array.from({ length: totalPages }, (_, index) => index + 1);
  }
  if (currentPage <= 3) return [1, 2, 3, "...", totalPages];
  if (currentPage >= totalPages - 2) return [1, "...", totalPages - 2, totalPages - 1, totalPages];
  return [1, "...", currentPage - 1, currentPage, currentPage + 1, "...", totalPages];
}

export function Pagination({
  as = "footer",
  ariaLabel,
  className,
  currentPage,
  disabled = false,
  ellipsis = "...",
  pageSize,
  pageSizeAriaLabel,
  pageSizeOptions,
  spacedPageSizeLabel = false,
  splitTotal = false,
  total,
  totalPages,
  totalUnit,
  onPageChange,
  onPageSizeChange,
}: Props) {
  const Root = as;
  const paginationItems = getPaginationItems(currentPage, totalPages);
  const rootClassName = ["pagination-row", className].filter(Boolean).join(" ");

  return (
    <Root className={rootClassName}>
      <div className="pagination-total">
        {splitTotal ? (
          <>
            <span>共</span>
            <strong>{total}</strong>
            <span>{totalUnit}</span>
          </>
        ) : (
          <span>共 <strong>{total}</strong> {totalUnit}</span>
        )}
        <select
          aria-label={pageSizeAriaLabel}
          value={pageSize}
          onChange={(event) => onPageSizeChange(Number(event.target.value))}
        >
          {pageSizeOptions.map((value) => (
            <option key={value} value={value}>
              {value}{spacedPageSizeLabel ? " " : ""}条/页
            </option>
          ))}
        </select>
      </div>
      <nav aria-label={ariaLabel}>
        <button type="button" disabled={disabled || currentPage <= 1} onClick={() => onPageChange(currentPage - 1)}>
          上一页
        </button>
        {paginationItems.map((item, index) =>
          item === "..." ? (
            <span className="pagination-ellipsis" key={`ellipsis-${index}`}>
              {ellipsis}
            </span>
          ) : item === currentPage ? (
            <strong key={item}>{item}</strong>
          ) : (
            <button type="button" key={item} disabled={disabled} onClick={() => onPageChange(item)}>
              {item}
            </button>
          ),
        )}
        <button type="button" disabled={disabled || currentPage >= totalPages} onClick={() => onPageChange(currentPage + 1)}>
          下一页
        </button>
      </nav>
    </Root>
  );
}
