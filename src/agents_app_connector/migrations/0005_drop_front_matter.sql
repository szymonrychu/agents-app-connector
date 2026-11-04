-- The translator writes its document through document_write, which never adds front matter;
-- the reply-as-document path that detected it is gone.
ALTER TABLE specs DROP COLUMN front_matter;
