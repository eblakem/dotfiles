# Thursday, 2nd Oct 2025

## Times

Start: 0700
Finish: 

## Events

- 0930 snomed ct training [[snomed-ct-training]]

## Tasks

[x] 8.19 dev deployment
    - db deploy broken, perhaps snapshot related, missing fk to patient maybe
    - yes, i had fk to demographic.patient but forgot to specify it as not null and instead just had patientId int (should be patientId int not null, with FK constraint)

[x] user expiry email check at 9am
    - 3 were sent yesterday
    - no extras sent today

[wip] lymphoma stuff reference [[friday-19th-sept-2025]]

[wip] [[ccq-unique]] final extract procedure
    - implicit arms don't record consent except for 'give caq permission'
    - send [[phoebe-woodrow]] the list of non-matches after it closes, and she will update the final selection sheet which can be imported into the database and used for matching records

[] ccq unique website disable
    - need to create webpage, detail in email, can deploy to staging perhaps so it is ready
