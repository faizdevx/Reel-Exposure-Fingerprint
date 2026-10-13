import type { FingerprintKind } from './api';

interface Props {
  value: FingerprintKind;
  onChange: (kind: FingerprintKind) => void;
}

const OPTIONS: readonly { kind: FingerprintKind; label: string }[] = [
  { kind: 'exposure', label: 'Exposure' },
  { kind: 'engagement', label: 'Engagement' },
];

/** The one filter of the History page: which fingerprint the sections below show. */
export function KindToggle({ value, onChange }: Props) {
  return (
    <fieldset className="toggle">
      <legend>Fingerprint</legend>
      {OPTIONS.map((option) => (
        <label key={option.kind} className={value === option.kind ? 'on' : undefined}>
          <input
            type="radio"
            name="fingerprint-kind"
            value={option.kind}
            checked={value === option.kind}
            onChange={() => onChange(option.kind)}
          />
          {option.label}
        </label>
      ))}
    </fieldset>
  );
}
