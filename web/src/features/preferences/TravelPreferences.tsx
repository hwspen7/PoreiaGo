import { useEffect, useMemo, useState } from 'react';
import {
  BadgeCheck,
  CalendarDays,
  Heart,
  Hotel,
  Info,
  MapPin,
  RotateCcw,
  Save,
  ShieldCheck,
  SlidersHorizontal,
  TrainFront,
  UtensilsCrossed,
  Wallet,
  Users,
} from 'lucide-react';
import { apiJSON } from '../../lib/api';
import './travelPreferences.css';

type TravelProfile = {
  travel_pace_schedule: string;
  interests_experiences: string[];
  budget_spending: string;
  transportation: string[];
  accommodation: string[];
  food_dining: string[];
  accessibility_essential_needs: string[];
  crowds_atmosphere: string[];
};
type SingleKey = 'travel_pace_schedule' | 'budget_spending';
type MultiKey = Exclude<keyof TravelProfile, SingleKey>;
type Option = { value: string; label: string; description: string };
type Section = {
  id: keyof TravelProfile;
  heading: string;
  description: string;
  icon: typeof Heart;
  multiple: boolean;
  options: readonly Option[];
};
type ProfileResponse = {
  profile: TravelProfile;
  saved: boolean;
  updated_at: string | null;
  unchanged?: boolean;
};

const DEFAULT_PROFILE: TravelProfile = {
  travel_pace_schedule: 'no_preference',
  interests_experiences: [],
  budget_spending: 'no_preference',
  transportation: [],
  accommodation: [],
  food_dining: [],
  accessibility_essential_needs: [],
  crowds_atmosphere: [],
};

const SECTIONS: readonly Section[] = [
  {
    id: 'travel_pace_schedule',
    heading: 'Travel Pace & Schedule',
    description: 'How full should a typical travel day feel?',
    icon: CalendarDays,
    multiple: false,
    options: [
      {
        value: 'relaxed',
        label: 'Relaxed',
        description: 'Leave time between stops for breaks and unplanned discoveries',
      },
      {
        value: 'balanced',
        label: 'Balanced',
        description: 'Strike a comfortable balance between activities and downtime',
      },
      {
        value: 'active',
        label: 'Active',
        description: 'Fit more sightseeing and experiences into each day',
      },
    ],
  },
  {
    id: 'interests_experiences',
    heading: 'Interests & Experiences',
    description: 'What would you most like to discover?',
    icon: Heart,
    multiple: true,
    options: [
      {
        value: 'history_culture',
        label: 'History & Culture',
        description: 'Include historic sites, local heritage, and cultural experiences',
      },
      {
        value: 'nature',
        label: 'Nature',
        description: 'Explore parks, green spaces, and scenic landscapes',
      },
      {
        value: 'museums_art',
        label: 'Museums & Art',
        description: 'Prioritize museums, galleries, and exhibitions',
      },
      {
        value: 'architecture',
        label: 'Architecture',
        description: 'Explore distinctive buildings and architectural landmarks',
      },
      {
        value: 'aviation',
        label: 'Aviation',
        description: 'Include aviation museums, aircraft exhibits, and related attractions',
      },
      {
        value: 'shopping',
        label: 'Shopping',
        description: 'Allow time for markets, independent shops, and shopping districts',
      },
      {
        value: 'outdoor_adventures',
        label: 'Outdoor Adventures',
        description: 'Include hikes and outdoor activities suited to the destination',
      },
      {
        value: 'entertainment',
        label: 'Entertainment',
        description: 'Make room for concerts, live performances, and shows',
      },
    ],
  },
  {
    id: 'budget_spending',
    heading: 'Budget & Spending',
    description: 'Your usual spending comfort level, not a fixed budget.',
    icon: Wallet,
    multiple: false,
    options: [
      {
        value: 'budget',
        label: 'Budget-friendly',
        description: 'Favor affordable stays, meals, and activities',
      },
      {
        value: 'mid_range',
        label: 'Mid-range',
        description: 'Balance comfort and quality with overall cost',
      },
      {
        value: 'premium',
        label: 'Premium',
        description: 'Prioritize added comfort and quality where they matter most',
      },
      {
        value: 'luxury',
        label: 'Luxury',
        description: 'Favor high-end accommodation, dining, and experiences',
      },
    ],
  },
  {
    id: 'transportation',
    heading: 'Transportation',
    description: 'Choose the ways you prefer to get around.',
    icon: TrainFront,
    multiple: true,
    options: [
      {
        value: 'walking',
        label: 'Walking',
        description: 'Explore on foot where walking routes are practical',
      },
      {
        value: 'public_transit',
        label: 'Public Transit',
        description: 'Use local trains, metro services, and buses where practical',
      },
      {
        value: 'rental_car',
        label: 'Rental Car',
        description: 'Keep the flexibility to explore at your own pace by car',
      },
      {
        value: 'taxi_rideshare',
        label: 'Taxi / Rideshare',
        description: 'Prefer direct transfers by taxi or rideshare',
      },
      {
        value: 'cycling',
        label: 'Cycling',
        description: 'Consider bike-friendly routes when conditions allow',
      },
    ],
  },
  {
    id: 'accommodation',
    heading: 'Accommodation',
    description: 'What kinds of places do you like staying in?',
    icon: Hotel,
    multiple: true,
    options: [
      {
        value: 'hotels',
        label: 'Hotels',
        description: 'Prefer hotels with familiar amenities and services',
      },
      {
        value: 'boutique_hotels',
        label: 'Boutique Hotels',
        description: 'Look for smaller hotels with distinctive character',
      },
      {
        value: 'apartments',
        label: 'Apartments',
        description: 'Favor self-contained stays with extra space and kitchen facilities',
      },
      {
        value: 'resorts',
        label: 'Resorts',
        description: 'Look for properties with leisure facilities on site',
      },
      {
        value: 'hostels',
        label: 'Hostels',
        description: 'Consider affordable stays with a social atmosphere',
      },
    ],
  },
  {
    id: 'food_dining',
    heading: 'Food & Dining',
    description: 'Food experiences and dietary requirements.',
    icon: UtensilsCrossed,
    multiple: true,
    options: [
      {
        value: 'local_cuisine',
        label: 'Local Cuisine',
        description: 'Explore regional dishes and local dining traditions',
      },
      {
        value: 'street_food',
        label: 'Street Food',
        description: 'Make time for street stalls and local food markets',
      },
      {
        value: 'casual_dining',
        label: 'Casual Dining',
        description: 'Favor relaxed cafes and informal restaurants',
      },
      {
        value: 'fine_dining',
        label: 'Fine Dining',
        description: 'Make room for fine dining and special-occasion meals',
      },
      {
        value: 'vegetarian',
        label: 'Vegetarian',
        description: 'Look for vegetarian dishes and confirm ingredients when needed',
      },
      {
        value: 'vegan',
        label: 'Vegan',
        description: 'Prioritize plant-based meals and confirm ingredients with venues',
      },
      {
        value: 'halal',
        label: 'Halal',
        description: 'Look for halal options and confirm preparation where necessary',
      },
      {
        value: 'kosher',
        label: 'Kosher',
        description: 'Look for kosher options and confirm preparation where necessary',
      },
    ],
  },
  {
    id: 'accessibility_essential_needs',
    heading: 'Accessibility & Essential Needs',
    description: 'Requirements that may affect routes, venues and timing.',
    icon: ShieldCheck,
    multiple: true,
    options: [
      {
        value: 'avoid_long_walks',
        label: 'Avoid Long Walks',
        description: 'Keep walking distances manageable throughout each day',
      },
      {
        value: 'step_free_access',
        label: 'Step-Free Access',
        description: 'Prioritize step-free routes and accessible entrances',
      },
      {
        value: 'wheelchair_accessible',
        label: 'Wheelchair Accessible',
        description: 'Prioritize wheelchair-accessible venues and transport',
      },
      {
        value: 'frequent_rest_breaks',
        label: 'Frequent Rest Breaks',
        description: 'Build regular rest stops into the daily schedule',
      },
    ],
  },
  {
    id: 'crowds_atmosphere',
    heading: 'Crowds & Atmosphere',
    description: 'The surroundings and atmosphere you enjoy.',
    icon: Users,
    multiple: true,
    options: [
      {
        value: 'quiet',
        label: 'Quiet & Peaceful',
        description: 'Favor peaceful settings away from busy tourist areas',
      },
      {
        value: 'local_authentic',
        label: 'Local & Authentic',
        description: 'Explore neighborhood spots and everyday local life',
      },
      {
        value: 'lively_social',
        label: 'Lively & Social',
        description: 'Look for lively districts and opportunities to socialize',
      },
      {
        value: 'avoid_crowds',
        label: 'Avoid Crowds',
        description: 'Favor quieter visiting times and less crowded places',
      },
    ],
  },
];

const NO_PREFERENCE_DESCRIPTION = 'Keep this flexible for each trip';

export function TravelPreferences({ userId }: { userId: string }) {
  const [profile, setProfile] = useState<TravelProfile>(DEFAULT_PROFILE);
  const [savedProfile, setSavedProfile] = useState<TravelProfile>(DEFAULT_PROFILE);
  const [saved, setSaved] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [consent, setConsent] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [hoveredOption, setHoveredOption] = useState<{
    sectionId: keyof TravelProfile;
    value: string;
  } | null>(null);
  const [error, setError] = useState('');
  const dirty = useMemo(
    () => JSON.stringify(profile) !== JSON.stringify(savedProfile),
    [profile, savedProfile],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    apiJSON<ProfileResponse>(`/api/travel-profile?user_id=${encodeURIComponent(userId)}`)
      .then((data) => {
        if (cancelled) return;
        setProfile(data.profile);
        setSavedProfile(data.profile);
        setSaved(data.saved);
        setUpdatedAt(data.updated_at);
        setConsent(false);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : 'Unable to load preferences.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  function showOptionDescription(sectionId: keyof TravelProfile, value: string) {
    setHoveredOption({ sectionId, value });
  }
  function hideOptionDescription(sectionId: keyof TravelProfile, value: string) {
    setHoveredOption((current) =>
      current?.sectionId === sectionId && current.value === value ? null : current,
    );
  }

  function selectSingle(key: SingleKey, value: string) {
    setProfile((prev) => ({ ...prev, [key]: value }));
    setNotice('');
  }
  function toggleMulti(key: MultiKey, value: string) {
    setProfile((prev) => {
      const selected = prev[key];
      return {
        ...prev,
        [key]: selected.includes(value)
          ? selected.filter((item) => item !== value)
          : [...selected, value],
      };
    });
    setNotice('');
  }
  function clearSection(section: Section) {
    if (section.multiple) {
      setProfile((prev) => ({ ...prev, [section.id]: [] }));
    } else {
      setProfile((prev) => ({ ...prev, [section.id]: 'no_preference' }));
    }
    setNotice('');
  }

  async function saveProfile() {
    if (!consent || !dirty || busy) return;
    setBusy(true);
    setNotice('');
    setError('');
    try {
      const result = await apiJSON<ProfileResponse>('/api/travel-profile', {
        method: 'PUT',
        body: JSON.stringify({ user_id: userId, consent: true, profile }),
      });
      setProfile(result.profile);
      setSavedProfile(result.profile);
      setSaved(result.saved);
      setUpdatedAt(result.updated_at);
      setConsent(false);
      setNotice('Your travel preferences have been saved.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to save preferences.');
    } finally {
      setBusy(false);
    }
  }

  async function resetProfile() {
    if (
      busy ||
      !window.confirm(
        'Clear your saved travel profile? Other saved preferences will not be affected.',
      )
    )
      return;
    setBusy(true);
    setNotice('');
    setError('');
    try {
      const result = await apiJSON<ProfileResponse>('/api/travel-profile/reset', {
        method: 'POST',
        body: JSON.stringify({ user_id: userId, confirmed: true }),
      });
      setProfile(result.profile);
      setSavedProfile(result.profile);
      setSaved(false);
      setUpdatedAt(null);
      setConsent(false);
      setNotice('Travel profile cleared.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to clear preferences.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dashboard travel-preferences-page">
      <div className="page-heading">
        <div className="eyebrow">YOUR TRAVEL PROFILE</div>
        <h1>Travel Preferences</h1>
        <p>
          Choose what matters to you. PoreiaGo uses your selections as defaults when planning future
          journeys.
        </p>
      </div>
      <div className="travel-pref-topline">
        <span className="travel-pref-status">
          {saved ? <BadgeCheck size={16} /> : <MapPin size={16} />}
          {loading ? 'Loading preferences...' : saved ? 'Profile saved' : 'No saved profile yet'}
        </span>
        {updatedAt && (
          <span className="travel-pref-date">
            Last saved{' '}
            {new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' }).format(
              new Date(updatedAt),
            )}
          </span>
        )}
      </div>
      <div className="travel-pref-grid">
        {SECTIONS.map((section, index) => {
          const Icon = section.icon;
          const current = profile[section.id];
          const isEmpty = Array.isArray(current)
            ? current.length === 0
            : current === 'no_preference';
          const currentHover = hoveredOption?.sectionId === section.id ? hoveredOption.value : null;
          const hoveredDetail =
            currentHover === 'no_preference'
              ? { label: 'No Preference', description: NO_PREFERENCE_DESCRIPTION }
              : section.options.find((option) => option.value === currentHover);
          return (
            <section key={section.id} className="travel-pref-card" aria-label={section.heading}>
              <div className="travel-pref-card-header">
                <span className="travel-pref-icon">
                  <Icon size={18} />
                </span>
                <span className="travel-pref-count">{String(index + 1).padStart(2, '0')}</span>
              </div>
              <h2>{section.heading}</h2>
              <p>{section.description}</p>
              <div className="travel-pref-pills" role="group" aria-label={section.heading}>
                <button
                  type="button"
                  className={'travel-pref-pill' + (isEmpty ? ' is-selected' : '')}
                  aria-pressed={isEmpty}
                  disabled={loading || busy}
                  aria-label={`No Preference: ${NO_PREFERENCE_DESCRIPTION}`}
                  onMouseEnter={() => showOptionDescription(section.id, 'no_preference')}
                  onMouseLeave={() => hideOptionDescription(section.id, 'no_preference')}
                  onFocus={() => showOptionDescription(section.id, 'no_preference')}
                  onBlur={() => hideOptionDescription(section.id, 'no_preference')}
                  onClick={() => clearSection(section)}
                >
                  No Preference
                </button>
                {section.options.map((option) => {
                  const active = Array.isArray(current)
                    ? current.includes(option.value)
                    : current === option.value;
                  return (
                    <button
                      type="button"
                      key={option.value}
                      className={'travel-pref-pill' + (active ? ' is-selected' : '')}
                      aria-pressed={active}
                      disabled={loading || busy}
                      aria-label={`${option.label}: ${option.description}`}
                      onMouseEnter={() => showOptionDescription(section.id, option.value)}
                      onMouseLeave={() => hideOptionDescription(section.id, option.value)}
                      onFocus={() => showOptionDescription(section.id, option.value)}
                      onBlur={() => hideOptionDescription(section.id, option.value)}
                      onClick={() =>
                        section.multiple
                          ? toggleMulti(section.id as MultiKey, option.value)
                          : selectSingle(section.id as SingleKey, option.value)
                      }
                    >
                      {option.label}
                    </button>
                  );
                })}
              </div>
              <div
                className={'travel-pref-option-help' + (hoveredDetail ? ' is-active' : '')}
                role="status"
                aria-live="polite"
                aria-atomic="true"
              >
                {hoveredDetail ? (
                  <>
                    <Info size={15} aria-hidden="true" />
                    <span>{hoveredDetail.description}</span>
                  </>
                ) : (
                  <span className="travel-pref-option-hint">
                    {section.multiple ? 'Select multiple options' : 'Select one option'} · Hover or
                    focus an option for details
                  </span>
                )}
              </div>
            </section>
          );
        })}
      </div>
      <div className="travel-pref-footer">
        <div className="travel-pref-consent-row">
          <label>
            <input
              type="checkbox"
              checked={consent}
              disabled={loading || busy}
              onChange={(event) => setConsent(event.target.checked)}
            />
            I agree to save these preferences for future journeys.
          </label>
          <p>
            Current trip instructions take priority. Essential needs should be verified for each
            destination.
          </p>
        </div>
        {error && (
          <div className="travel-pref-error" role="alert">
            {error}
          </div>
        )}
        {notice && (
          <div className="travel-pref-notice" role="status">
            {notice}
          </div>
        )}
        <div className="travel-pref-actions">
          <button
            className="travel-pref-reset"
            type="button"
            onClick={resetProfile}
            disabled={loading || busy || !saved}
          >
            <RotateCcw size={16} /> Clear Saved Profile
          </button>
          <button
            className="travel-pref-save"
            type="button"
            onClick={saveProfile}
            disabled={loading || busy || !dirty || !consent}
          >
            {busy ? <SlidersHorizontal size={16} /> : <Save size={16} />}
            {busy ? 'Saving...' : !dirty && saved ? 'Saved' : 'Save Preferences'}
          </button>
        </div>
      </div>
    </div>
  );
}
