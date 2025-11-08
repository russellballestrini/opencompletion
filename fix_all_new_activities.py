#!/usr/bin/env python3
"""
Fix completion issues in activities 30-36:
- Keep 'off_topic' looping on final step
- Remove next_section_and_step from all other buckets in final step to allow completion
"""
import yaml
import glob

def fix_final_step_completion(file_path):
    """Fix the final step to allow completion."""
    # Read file
    with open(file_path, 'r') as f:
        activity = yaml.safe_load(f)

    # Find the last section
    if not activity.get('sections'):
        return False

    last_section = activity['sections'][-1]
    last_section_id = last_section['section_id']

    # Find the last step in the last section
    if not last_section.get('steps'):
        return False

    last_step = last_section['steps'][-1]
    last_step_id = last_step['step_id']

    # Fix: Keep ONLY 'off_topic' looping, remove next_section_and_step from other transitions
    if 'transitions' not in last_step:
        return False

    modified = False
    for bucket, transition in last_step['transitions'].items():
        if bucket == 'off_topic':
            # Ensure off_topic loops (so validator doesn't consider step terminal)
            if 'next_section_and_step' not in transition or transition['next_section_and_step'] != f"{last_section_id}:{last_step_id}":
                transition['next_section_and_step'] = f"{last_section_id}:{last_step_id}"
                modified = True
        else:
            # Remove next_section_and_step from completion buckets
            if 'next_section_and_step' in transition:
                del transition['next_section_and_step']
                modified = True

    if modified:
        # Write back
        with open(file_path, 'w') as f:
            yaml.dump(activity, f, default_flow_style=False, sort_keys=False, width=1000, allow_unicode=True)
        return True

    return False

def main():
    files = [
        'research/activity30-logic-puzzles.yaml',
        'research/activity31-scientific-method.yaml',
        'research/activity32-world-geography.yaml',
        'research/activity33-environmental-science.yaml',
        'research/activity34-media-literacy.yaml',
        'research/activity35-american-history.yaml',
        'research/activity36-biblical-history.yaml',
    ]

    for file_path in files:
        if fix_final_step_completion(file_path):
            print(f"✅ Fixed {file_path}")
        else:
            print(f"⚠️  No changes needed for {file_path}")

if __name__ == '__main__':
    main()
