import { type ReactNode } from 'react';

const DEFAULT_VALUE_CLASS = 'text-dark-100';

interface StatCardProps {
  label: string;
  value: string | number;
  icon?: ReactNode;
  valueClassName?: string;
}

export function StatCard({
  label,
  value,
  icon,
  valueClassName = DEFAULT_VALUE_CLASS,
}: StatCardProps) {
  return (
    <div className="rounded-xl bg-dark-800/30 p-3">
      <div className="flex items-center gap-1.5 text-sm text-dark-500">
        {icon}
        <span>{label}</span>
      </div>
      <div className={`mt-1 truncate text-base font-semibold sm:text-lg ${valueClassName}`}>
        {value}
      </div>
    </div>
  );
}
