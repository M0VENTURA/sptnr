-- Fix track_number column type from bigint to VARCHAR
-- Track numbers can be "1/12" format (track 1 of 12), not just integers

-- Step 1: Add new temporary column
ALTER TABLE tracks ADD COLUMN track_number_new VARCHAR(50);

-- Step 2: Copy data, converting bigint to string
UPDATE tracks SET track_number_new = CAST(track_number AS VARCHAR) WHERE track_number IS NOT NULL;

-- Step 3: Drop old column
ALTER TABLE tracks DROP COLUMN track_number;

-- Step 4: Rename new column to original name
ALTER TABLE tracks RENAME COLUMN track_number_new TO track_number;
