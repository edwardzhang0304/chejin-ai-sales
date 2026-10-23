import React from "react";
import { createPortal } from "react-dom";
import {
  BarChart3,
  CheckCheck,
  CircleUserRound,
  Layers3,
  Activity,
} from "lucide-react";

type SurfaceProps = React.HTMLAttributes<HTMLElement> & {
  as?: React.ElementType;
};
type SurfaceTag = keyof React.JSX.IntrinsicElements;

function surface<DefaultTag extends SurfaceTag>(
  name: string,
  defaultTag: DefaultTag,
) {
  return function Surface<Tag extends SurfaceTag = DefaultTag>({
    as,
    className = "",
    ...props
  }: { as?: Tag } & React.ComponentPropsWithoutRef<Tag>) {
    return React.createElement(as || defaultTag, {
      ...props,
      className: `ui-${name} ${className}`,
    });
  };
}

// These shared slots own geometry; feature modules retain data and action handlers.
export const PageHeader = surface("page-header", "header");
export const ListSurface = surface("list-surface", "section");
export const ListHeader = surface("list-header", "header");
export const FilterBar = surface("filter-bar", "div");
export const TableSurface = surface("table-surface", "div");
export const DetailDrawer = surface("detail-drawer", "aside");
export const DrawerContent = surface("drawer-content", "div");
export const DrawerFooter = surface("drawer-footer", "footer");
export const DrawerHeader = surface("drawer-header", "header");
export const DialogSurface = surface("dialog", "section");

// A top-level layer avoids the fixed-position containment created by glass drawers.
export function ModalLayer(props: React.HTMLAttributes<HTMLDivElement>) {
  return createPortal(<div {...props} />, document.body);
}

const metricIcons = [Layers3, CheckCheck, CircleUserRound, BarChart3, Activity];
export function MetricStrip({
  className = "",
  children,
  ...props
}: SurfaceProps) {
  return (
    <section {...props} className={`ui-metric-strip ${className}`}>
      {React.Children.map(children, (child, index) => {
        if (!React.isValidElement<{ children?: React.ReactNode }>(child))
          return child;
        const Icon = metricIcons[index % metricIcons.length];
        return React.cloneElement(
          child,
          {},
          <>
            <span className="ui-metric-icon">
              <Icon size={23} />
            </span>
            <div className="ui-metric-copy">{child.props.children}</div>
          </>,
        );
      })}
    </section>
  );
}
