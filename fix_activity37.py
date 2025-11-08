#!/usr/bin/env python3
"""
Fix activity37:
1. Change 'close' bucket to NOT advance (retry same step)
2. Keep 'off_topic' looping on final step, remove next_section_and_step from completion buckets
"""
import yaml

def fix_activity37():
    file_path = "research/activity37-programming-languages.yaml"

    # Read original file
    with open(file_path, 'r') as f:
        activity = yaml.safe_load(f)

    # Fix 1: Change ALL "close" transitions to stay on same step
    for section in activity['sections']:
        section_id = section['section_id']
        for step in section['steps']:
            step_id = step['step_id']
            if 'transitions' in step and 'close' in step.get('buckets', []):
                # Change 'close' to stay on same step (don't advance)
                if 'close' in step['transitions']:
                    step['transitions']['close']['next_section_and_step'] = f"{section_id}:{step_id}"

    # Fix 2: For final step (conclusion:step_1), keep ONLY 'off_topic' looping
    # Remove next_section_and_step from all other transitions to allow completion
    for section in activity['sections']:
        if section['section_id'] == 'conclusion':
            for step in section['steps']:
                if step['step_id'] == 'step_1':
                    for bucket, transition in step['transitions'].items():
                        # Remove next_section_and_step from all except off_topic
                        if bucket != 'off_topic' and 'next_section_and_step' in transition:
                            del transition['next_section_and_step']
                    # Ensure off_topic loops (for validator to not consider it terminal)
                    if 'off_topic' in step['transitions']:
                        step['transitions']['off_topic']['next_section_and_step'] = 'conclusion:step_1'

    # Write back with minimal formatting changes
    with open(file_path, 'w') as f:
        yaml.dump(activity, f, default_flow_style=False, sort_keys=False, width=1000, allow_unicode=True)

    print(f"✅ Fixed {file_path}")
    print("  - 'close' buckets now retry same step (don't advance)")
    print("  - Final step can now complete (off_topic loops, others complete)")

if __name__ == '__main__':
    fix_activity37()
