import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.naive_bayes import MultinomialNB
from sklearn.metrics import accuracy_score, classification_report

data = {
    'email':[
        "Congratulation! You've won a lottery. Click here to claim.",
        "Important : Update your payment information.",
        "Update your bank KYC. Click here.",
        "Urgent : Verify your account immediately!",
        "Meet your new colleague at the office.",
        "Hello! just checking in to see how you're doing.",
        "Your invoice from our recent purchase is attached."
    ],
    'label':[1,0,1,0,1,0,1]
}

df = pd.DataFrame(data)

print('Dataset: ')
print(df)

x = df['email']
y = df['label']

x_train, x_test, y_train, y_test = train_test_split(x,y,test_size=0.2, random_state=42)
vector = CountVectorizer()
x_train_vector = vector.fit_transform(x_train)
x_test_vector = vector.transform(x_test)

model = MultinomialNB()
model.fit(x_train_vector,y_train)

y_pred = model.predict(x_test_vector)

accuracy = accuracy_score(y_test, y_pred)
report = classification_report(y_test, y_pred)

print(f"\n Accuracy: {accuracy: .2f}")
print("Classification Report : ")
print(report)

