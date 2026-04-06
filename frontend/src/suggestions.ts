// Consolidated from docs/sample-questions.md + additional suggestions.
// Used for randomized welcome-screen bubbles.

export const QA_SUGGESTIONS = [
  // Admissions & requirements
  'What GPA and SAT scores do I need to get into Stanford?',
  'Is MIT test-optional for the 2026 admissions cycle?',
  'Does UCLA require letters of recommendation?',
  'What extracurriculars does Harvard look for in applicants?',
  'How many applicants does Duke receive each year and what\'s the acceptance rate?',
  'What are the admission requirements for Columbia?',
  'What ACT score is competitive for Northwestern?',
  'How hard is it to get into Cornell?',

  // Deadlines & application process
  'When is the Early Decision deadline for Columbia?',
  'Can I apply Early Action to both MIT and Harvard?',
  'What documents do I need to submit for the Common App to Northwestern?',
  'Does Georgetown have its own application or use the Common App?',
  'When is the Regular Decision deadline for Harvard?',
  'What are the application deadlines for UC schools?',

  // Financial aid & scholarships
  'What merit scholarships does USC offer for incoming freshmen?',
  'How do I apply for financial aid at Princeton?',
  'What is the total cost of attendance at NYU including room and board?',
  'Does Stanford meet 100% of demonstrated financial need?',
  'What is the FAFSA deadline for University of Michigan?',
  'Does MIT offer merit scholarships?',
  'What is the average financial aid package at Yale?',
  'Best scholarships for CS majors?',
  'How do I apply for FAFSA?',

  // Academics & programs
  'What majors does Carnegie Mellon\'s School of Computer Science offer?',
  'Can I double major at UPenn?',
  'Does Caltech offer a pre-med track?',
  'How large are typical class sizes at Williams College?',
  'What study abroad programs does Georgetown have?',
  'How is the CS program at Carnegie Mellon?',
  'What is the student-faculty ratio at Caltech?',
  'Does UPenn have a dual degree program?',
  'What pre-med programs are best?',
  'Best engineering programs in the US?',

  // Campus life & housing
  'Are freshmen required to live on campus at Yale?',
  'What meal plan options does UCLA offer?',
  'What Division I sports does Duke have?',
  'How many student clubs and organizations are there at UMich?',
  'Does Rice have a residential college system?',
  'What is campus life like at UMich?',
  'How diverse is Vanderbilt?',

  // Career outcomes
  'What is the job placement rate for Stanford CS graduates?',
  'Where do Harvard graduates typically work after graduation?',
  'What internship opportunities does Wharton provide for undergrads?',
  'What is the average starting salary for MIT engineering graduates?',

  // International students
  'Does Columbia offer financial aid to international students?',
  'What English proficiency tests does UCLA accept (TOEFL, IELTS)?',
  'What visa support does MIT provide for admitted international students?',

  // Transfer students
  'What is the transfer acceptance rate at USC?',
  'How many credits will transfer from a community college to UC Berkeley?',
  'What GPA do I need to transfer into Cornell?',

  // Campus safety & wellness
  'What mental health resources does Yale offer to undergraduates?',
  'How safe is the area around UChicago\'s campus?',
  'What disability accommodations does Stanford provide?',

  // Diversity & inclusion
  'How diverse is the student body at Rice University?',
  'What multicultural student organizations does Brown have?',
  'What support does Duke offer for first-generation college students?',

  // Comparisons
  'How does MIT compare to Caltech for undergraduate engineering?',
  'Stanford vs Harvard: which has better financial aid?',
  'What are the differences between UPenn and NYU for business?',
  'Compare the campus life at Duke vs UNC Chapel Hill.',
  'Which has a higher acceptance rate, UCLA or UC Berkeley?',
  'UChicago vs Northwestern for economics?',

  // Chance-me / admission prediction
  'What are my chances at Stanford with a 3.9 GPA and 1520 SAT?',
  'Can I get into MIT with a 3.7 GPA and 35 ACT?',
  'Do I have a chance at Harvard with a 4.0 GPA, 1550 SAT, and strong research?',
  'What are my odds at Duke with a 3.8 GPA and 1480 SAT?',
  'Will I get into UCLA with a 3.5 GPA and 1400 SAT?',

  // Application strategy
  'How many colleges should I apply to?',
  'Should I apply Early Decision?',
  'What are safety schools for a 3.8 GPA?',
  'How important are extracurriculars?',
  'Do recommendation letters matter?',
  'What do admissions officers look for?',
]

export const ESSAY_SUGGESTIONS = [
  // Brainstorming — Why school
  'Help me brainstorm ideas for my Why Stanford essay.',
  'What makes UChicago unique that I could write about in my supplemental essay?',
  'I\'m interested in biomedical engineering — how can I connect that to Duke\'s programs in my essay?',
  'What are some good angles for a Why UPenn essay if I want to study business and engineering?',
  'What should I emphasize in my Why Columbia essay as someone interested in the Core Curriculum?',
  'I want to write about community — what specific things at Rice could I mention?',
  'Help me with my "Why Stanford?" essay',
  'Ideas for my Yale supplemental essay',

  // Brainstorming — general
  'Help me come up with ideas for my Common App personal statement about overcoming a challenge.',
  'Give me essay ideas for the UC prompt about a creative skill or talent.',
  'What should I write about for Common App?',
  'How do I pick a Common App prompt?',
  'Brainstorm personal statement topics',
  'Help me find my unique angle for college essays',
  'What makes an essay stand out?',
  'How do I start my college essay?',

  // Brainstorming — specific topics
  'How do I write about a challenge I overcame?',
  'Can I write about my immigrant experience?',
  'Essay ideas about community service',
  'How to write about a passion for music',
  'Should I write about my sports injury?',
  'How do I write about research I did?',

  // Review
  'Review my Why MIT essay — I wrote about my robotics project and how it connects to MIT\'s UROP program.',
  'Here is my Common App essay about growing up in two cultures. Does the narrative flow well?',
  'Can you give feedback on my Why Yale essay? I focused on the residential college system.',
  'I wrote about my passion for environmental science in my Stanford essay. Is it specific enough?',
  'Review my supplemental essay for Georgetown about the value of a liberal arts education.',
  'Here is my UC essay about leadership. Does it show enough personal growth?',
  'Critique my Why Brown essay — I wrote about the Open Curriculum and interdisciplinary interests.',

  // Strategy & structure
  'How long should my essay be?',
  'Should I use humor in my essay?',
  'How personal should my college essay be?',
  'What topics should I avoid in essays?',
  'How do I show rather than tell?',
  'How do I end my college essay?',
  'Is my essay too generic?',
  'Help me cut my essay down to 650 words',
]

/** Pick `count` random items from a list without repeats. */
export function pickRandom<T>(list: T[], count: number): T[] {
  const shuffled = [...list].sort(() => Math.random() - 0.5)
  return shuffled.slice(0, count)
}
