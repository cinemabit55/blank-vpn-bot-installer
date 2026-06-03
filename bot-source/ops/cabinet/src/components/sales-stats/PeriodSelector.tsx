import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { SALES_STATS } from '../../constants/salesStats';

interface PeriodSelectorProps {
  value: { days?: number; startDate?: string; endDate?: string };
  onChange: (period: { days?: number; startDate?: string; endDate?: string }) => void;
}

export function PeriodSelector({ value, onChange }: PeriodSelectorProps) {
  const { t } = useTranslation();
  const [showCustom, setShowCustom] = useState(false);

  const presetLabels: Record<number, string> = {
    7: t('admin.salesStats.period.week'),
    30: t('admin.salesStats.period.month'),
    90: t('admin.salesStats.period.quarter'),
    0: t('admin.salesStats.period.all'),
  };

  const handlePreset = (days: number) => {
    setShowCustom(false);
    onChange({ days });
  };

  const handleCustomToggle = () => {
    setShowCustom((prev) => !prev);
  };

  const handleDateChange = (field: 'startDate' | 'endDate', dateStr: string) => {
    onChange({
      ...value,
      days: undefined,
      [field]: dateStr,
    });
  };

  const isPresetActive = (days: number) => !showCustom && value.days === days;

  return (
    <div className="flex flex-wrap items-center gap-2">
      {SALES_STATS.PERIOD_PRESETS.map((days) => (
        <button
          key={days}
          type="button"
          onClick={() => handlePreset(days)}
          className={`rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
            isPresetActive(days)
              ? 'bg-accent-500/20 text-accent-400'
              : 'bg-dark-800/50 text-dark-400 hover:bg-dark-700/50 hover:text-dark-300'
          }`}
        >
          {presetLabels[days]}
        </button>
      ))}

      <button
        type="button"
        onClick={handleCustomToggle}
        className={`rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
          showCustom
            ? 'bg-accent-500/20 text-accent-400'
            : 'bg-dark-800/50 text-dark-400 hover:bg-dark-700/50 hover:text-dark-300'
        }`}
      >
        {t('admin.salesStats.period.custom')}
      </button>

      {showCustom && (
        <div className="flex items-center gap-2">
          <input
            type="date"
            value={value.startDate || ''}
            onChange={(e) => handleDateChange('startDate', e.target.value)}
            className="rounded-lg border border-dark-600 bg-dark-800 px-2 py-1 text-sm text-dark-200"
          />
          <span className="text-dark-500">{'\u2014'}</span>
          <input
            type="date"
            value={value.endDate || ''}
            onChange={(e) => handleDateChange('endDate', e.target.value)}
            className="rounded-lg border border-dark-600 bg-dark-800 px-2 py-1 text-sm text-dark-200"
          />
        </div>
      )}
    </div>
  );
}
