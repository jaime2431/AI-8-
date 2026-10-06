-- Run in the 'clean' database of sql-datia-ferrenort-x.database.windows.net (Azure portal > Query editor, signed in as a member of the SQL admin group)
CREATE USER [id-datia-ferrenort] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [id-datia-ferrenort];
ALTER ROLE db_datawriter ADD MEMBER [id-datia-ferrenort];
ALTER ROLE db_ddladmin ADD MEMBER [id-datia-ferrenort];
