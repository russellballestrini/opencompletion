# Planning Document: 5 New Educational Activities (No Python Scripts)

## Design Criteria

All activities should:
1. **No embedded Python** - Use only YAML features (buckets, transitions, metadata operations, AI feedback)
2. **Educational value** - Teach concepts through interaction and reflection
3. **Engaging** - Mix of narrative, problem-solving, and critical thinking
4. **Progressive** - Build knowledge step-by-step
5. **Use AI effectively** - Leverage AI categorization and personalized feedback
6. **Follow schema** - Validate against existing yaml validator

## Proposed Activities

### Activity 30: Critical Thinking & Logic Puzzles
**Topic**: Logical reasoning and deductive thinking
**Format**: Progressive logic puzzles with explanations

**Educational Goals**:
- Teach logical reasoning patterns (if-then, contrapositive, modus ponens)
- Practice deductive thinking
- Identify logical fallacies

**Mechanics**:
- Present logic puzzles of increasing difficulty
- Use buckets: `correct`, `partial_understanding`, `logical_error`, `off_topic`
- Use metadata to track: `puzzles_solved`, `hints_used`
- AI provides explanations for wrong answers
- No Python needed - pure question/answer with branching

**Example Flow**:
1. Introduction to logical statements
2. Simple syllogism puzzle
3. Truth table puzzle
4. Knights and knaves puzzle
5. Final complex logic puzzle

---

### Activity 31: Scientific Method Explorer
**Topic**: Understanding the scientific method through case studies
**Format**: Interactive investigation of famous scientific discoveries

**Educational Goals**:
- Learn the steps of the scientific method
- Apply hypothesis testing
- Understand experimental design
- Recognize bias and controls

**Mechanics**:
- Present historical scientific scenarios (e.g., Pasteur's germ theory, Newton's optics)
- Ask students to predict next steps
- Use buckets: `correct_method`, `skipped_step`, `biased_approach`, `creative_thinking`
- Metadata tracks: `experiments_designed`, `controls_identified`
- AI feedback explains scientific reasoning

**Example Flow**:
1. Introduction to scientific method steps
2. Case study: Design an experiment
3. Identify variables and controls
4. Analyze results
5. Draw conclusions and suggest follow-ups

---

### Activity 32: World Geography & Cultural Awareness
**Topic**: Geography with cultural and historical context
**Format**: Virtual travel journey with decision points

**Educational Goals**:
- Learn world geography (continents, countries, capitals)
- Understand cultural diversity and customs
- Explore historical connections between regions
- Develop global awareness

**Mechanics**:
- Choose-your-own-adventure style journey through continents
- At each location, learn facts and answer questions
- Use buckets: `correct`, `close_geography`, `confused_region`, `off_topic`
- Metadata tracks: `countries_visited`, `cultural_facts_learned`, `quiz_score`
- Use `metadata_tmp_random` to randomize quiz questions
- Multiple paths to complete the journey

**Example Flow**:
1. Choose starting continent
2. Learn about first country (history, culture, geography)
3. Quiz question about the location
4. Choose next destination (neighboring countries)
5. Collect "cultural insights" as metadata
6. Final reflection on global connections

---

### Activity 33: Environmental Science & Sustainability
**Topic**: Climate change, ecosystems, and sustainable practices
**Format**: Role-playing as environmental consultant

**Educational Goals**:
- Understand ecosystem interdependencies
- Learn about carbon footprint and climate impact
- Explore renewable energy options
- Practice systems thinking

**Mechanics**:
- Scenario-based decision making (e.g., city planning, company sustainability)
- Each decision affects "environmental_score" via metadata
- Use buckets: `sustainable_choice`, `mixed_impact`, `unsustainable`, `needs_more_info`
- Track metadata: `carbon_reduced`, `biodiversity_protected`, `decisions_made`
- AI explains environmental impacts of choices
- Multiple endings based on cumulative score

**Example Flow**:
1. Introduction to scenario (e.g., redesigning a city district)
2. Analyze current environmental problems
3. Make decisions on transportation, energy, green space
4. See immediate and long-term impacts
5. Reflect on tradeoffs and optimization
6. Final sustainability report based on choices

---

### Activity 34: Media Literacy & Information Evaluation
**Topic**: Evaluating sources, detecting misinformation, critical media consumption
**Format**: Interactive news/social media simulator

**Educational Goals**:
- Identify credible vs unreliable sources
- Recognize bias and propaganda techniques
- Understand fact-checking methods
- Develop healthy media consumption habits

**Mechanics**:
- Present various "articles" or "social media posts" (in content_blocks)
- Ask students to evaluate credibility
- Use buckets: `correctly_identified`, `partially_correct`, `missed_red_flags`, `overly_skeptical`
- Track metadata: `misinformation_detected`, `sources_verified`, `bias_identified`
- AI provides feedback on evaluation reasoning
- Progressive difficulty (obvious fake news → subtle bias)

**Example Flow**:
1. Introduction to media literacy concepts
2. Practice: Evaluate an obviously fake article
3. Identify bias in a real news article
4. Fact-check claims using described sources
5. Analyze social media manipulation techniques
6. Create a personal media literacy checklist

---

## Selected Activities Summary

| Activity | Number | Topic | Difficulty | Learning Style |
|----------|--------|-------|------------|----------------|
| Logic Puzzles | 30 | Critical Thinking | Medium | Problem-Solving |
| Scientific Method | 31 | Science Process | Medium | Case-Study |
| World Geography | 32 | Geography/Culture | Easy-Medium | Exploration |
| Environmental Science | 33 | Sustainability | Medium-Hard | Decision-Making |
| Media Literacy | 34 | Information Skills | Medium | Evaluation |

## Diversity Achieved

- **Subject Areas**: Logic, Science, Geography, Environmental Science, Media
- **Interaction Types**: Puzzles, Case Studies, Choose-Adventure, Role-Play, Evaluation
- **Skills Developed**: Reasoning, Scientific thinking, Cultural awareness, Systems thinking, Critical evaluation
- **Difficulty Range**: Easy-Medium to Medium-Hard
- **All achievable without Python scripts** - using metadata operations, AI categorization, and branching

## Implementation Notes

For all activities:
- Include `set_language` bucket in first step
- Use Socratic buckets (`correct`, `partial_understanding`, `limited_effort`)
- Provide encouraging AI feedback
- Use `tokens_for_ai_rubric` for final evaluation
- Track progress with metadata (scores, items collected, decisions made)
- Allow for multiple attempts per question (use `default_max_attempts_per_step: 3`)
- Include reflective final steps

## Next Steps

1. Implement activity30-logic-puzzles.yaml
2. Implement activity31-scientific-method.yaml
3. Implement activity32-world-geography.yaml
4. Implement activity33-environmental-science.yaml
5. Implement activity34-media-literacy.yaml
6. Validate all yamls using `make validate-yaml`
7. Write functional tests for at least 2 activities
8. Update documentation if needed
