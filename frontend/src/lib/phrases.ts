/** Home-screen greetings and the rotating "working" words shown while Qwen is busy. */

type DayPart = 'morning' | 'afternoon' | 'evening' | 'night'

export function dayPart(h: number): DayPart {
  if (h >= 5 && h < 12) return 'morning'
  if (h >= 12 && h < 17) return 'afternoon'
  if (h >= 17 && h < 22) return 'evening'
  return 'night'
}

const GREETINGS: Record<DayPart, ((n: string) => string)[]> = {
  morning: [
    (n) => `Good morning${n}`,
    (n) => `Morning${n}. What are we building today?`,
    () => 'Coffee and Qwen time?',
    (n) => `Fresh start${n}?`,
  ],
  afternoon: [
    (n) => `Good afternoon${n}`,
    () => 'How can I help this afternoon?',
    (n) => `Back at it${n}?`,
    () => 'What’s next on the list?',
  ],
  evening: [
    (n) => `Good evening${n}`,
    () => 'What’s on your mind this evening?',
    (n) => `Evening${n}. How can I help?`,
    () => 'Wrapping up the day?',
  ],
  night: [
    (n) => `Burning the midnight oil${n}?`,
    () => 'Up late? Let’s get it done.',
    (n) => `Late-night session${n}?`,
  ],
}

/** `seed` keeps the phrasing stable while the page is open; it only changes with the time of day. */
export function greeting(name: string | undefined, date: Date, seed: number): string {
  const options = GREETINGS[dayPart(date.getHours())]
  const first = name?.trim().split(/\s+/)[0]
  return options[seed % options.length](first ? `, ${first}` : '')
}

export const WORKING_WORDS = [
  'Thinking', 'Pondering', 'Mulling it over', 'Connecting the dots', 'Reasoning', 'Weighing the options',
  'Piecing it together', 'Brewing ideas', 'Crunching', 'Untangling', 'Sketching an answer', 'Cross-checking',
  'Gathering thoughts', 'Warming up the Sparks', 'Consulting the cluster', 'Noodling', 'Distilling',
  'Synthesising', 'Working it out', 'Polishing', 'Considering', 'Tinkering', 'Cooking something up',
]
